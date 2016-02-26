# Copyright 2014 iNuron NV
#
# Licensed under the Open vStorage Modified Apache License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.openvstorage.org/license
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
ScheduledTaskController module
"""

import copy
import time
from celery.result import ResultSet
from celery.schedules import crontab
from ConfigParser import RawConfigParser
from datetime import datetime
from datetime import timedelta
from ovs.celery_run import celery
from ovs.dal.hybrids.diskpartition import DiskPartition
from ovs.dal.hybrids.vdisk import VDisk
from ovs.dal.lists.storagedriverlist import StorageDriverList
from ovs.dal.lists.vdisklist import VDiskList
from ovs.dal.lists.vmachinelist import VMachineList
from ovs.dal.lists.servicelist import ServiceList
from ovs.extensions.db.arakoon.ArakoonInstaller import ArakoonClusterConfig
from ovs.extensions.db.arakoon.pyrakoon.tools.admin import ArakoonClientConfig, ArakoonAdminClient
from ovs.extensions.db.etcd.configuration import EtcdConfiguration
from ovs.extensions.generic.sshclient import SSHClient
from ovs.extensions.generic.sshclient import UnableToConnectException
from ovs.extensions.generic.system import System
from ovs.lib.helpers.decorators import ensure_single
from ovs.lib.mdsservice import MDSServiceController
from ovs.lib.vdisk import VDiskController
from ovs.lib.vmachine import VMachineController
from ovs.log.logHandler import LogHandler
from StringIO import StringIO
from time import mktime
from volumedriver.storagerouter import storagerouterclient

logger = LogHandler.get('lib', name='scheduled tasks')
storagerouterclient.Logger.setupLogging(LogHandler.load_path('storagerouterclient'))
# noinspection PyArgumentList
storagerouterclient.Logger.enableLogging()

SCRUBBER_LOGFILE_LOCATION = '/var/log/upstart/ovs-scrubber.log'

class ScheduledTaskController(object):
    """
    This controller contains all scheduled task code. These tasks can be
    executed at certain intervals and should be self-containing
    """

    @staticmethod
    @celery.task(name='ovs.scheduled.snapshot_all_vms', schedule=crontab(minute='0', hour='2-22'))
    @ensure_single(task_name='ovs.scheduled.snapshot_all_vms', extra_task_names=['ovs.scheduled.delete_snapshots'])
    def snapshot_all():
        """
        Snapshots all VMachines
        """
        logger.info('[SSA] Started')
        if len(Configuration.get('ovs.plugins.snapshot')) > 0:
            logger.info('[SSA] Build-in snapshotting aborted. Snapshot plugin registered')
            return

        success = []
        fail = []
        machines = VMachineList.get_customer_vmachines()
        for machine in machines:
            try:
                VMachineController.snapshot(machineguid=machine.guid,
                                            label='',
                                            is_consistent=False,
                                            is_automatic=True,
                                            is_sticky=False)
                success.append(machine.guid)
            except:
                fail.append(machine.guid)
        logger.info('[SSA] Snapshots have been taken for {0} vMachines, {1} failed.'.format(len(success), len(fail)))

    @staticmethod
    @celery.task(name='ovs.scheduled.delete_snapshots', schedule=crontab(minute='1', hour='2'))
    @ensure_single(task_name='ovs.scheduled.delete_snapshots')
    def delete_snapshots():
        """
        Delete snapshots policy

        Implemented delete snapshot policy:
        < 1d | 1d bucket | 1 | best of bucket   | 1d
        < 1w | 1d bucket | 6 | oldest of bucket | 7d = 1w
        < 1m | 1w bucket | 3 | oldest of bucket | 4w = 1m
        > 1m | delete

        :param timestamp: Timestamp to determine whether snapshots should be kept or not, if none provided, current time will be used
        """
        logger.info('Delete snapshots started')
        if len(Configuration.get('ovs.plugins.snapshot')) > 0:
            logger.info('Delete snapshots aborted. Snapshot plugin registered')
            return

        mark = time.time() + (7 * 24 * 60 * 60)  # All snapshots are removed after 7 days
        for vmachine in VMachineList.get_customer_vmachines():
            if any(vd.info['object_type'] in ['BASE'] for vd in vmachine.vdisks):
                for snapshot in vmachine.snapshots:
                    if int(snapshot['timestamp']) < mark:
                        for diskguid, snapshotguid in snapshot['snapshots'].iteritems():
                            VDiskController.delete_snapshot(diskguid=diskguid,
                                                            snapshotid=snapshotguid)
        for vdisk in VDiskList.get_without_vmachine():
            if vdisk.info['object_type'] in ['BASE']:
                for snapshot in vdisk.snapshots:
                    if int(snapshot['timestamp']) < mark:
                        VDiskController.delete_snapshot(diskguid=vdisk.guid,
                                                        snapshotid=snapshot['guid'])
        logger.info('Delete snapshots finished')

    @staticmethod
    @celery.task(name='ovs.scheduled.gather_scrub_work', schedule=crontab(minute='0', hour='3'))
    @ensure_single(task_name='ovs.scheduled.gather_scrub_work')
    def gather_scrub_work():
        """
        Retrieve and execute scrub work
        :return: None
        """
        logger.info('Gather Scrub - Started')

        scrub_locations = {}
        for storage_driver in StorageDriverList.get_storagedrivers():
            for partition in storage_driver.partitions:
                if DiskPartition.ROLES.SCRUB == partition.role:
                    logger.info('Gather Scrub - Storage Router {0:<15} has SCRUB partition at {1}'.format(storage_driver.storagerouter.ip, partition.path))
                    if storage_driver.storagerouter not in scrub_locations:
                        try:
                            _ = SSHClient(storage_driver.storagerouter)
                            scrub_locations[storage_driver.storagerouter] = str(partition.path)
                        except UnableToConnectException:
                            logger.warning('Gather Scrub - Storage Router {0:<15} is not reachable'.format(storage_driver.storagerouter.ip))

        if len(scrub_locations) == 0:
            raise RuntimeError('No scrub locations found')

        vdisk_guids = set()
        for vmachine in VMachineList.get_customer_vmachines():
            for vdisk in vmachine.vdisks:
                if vdisk.info['object_type'] == 'BASE':
                    vdisk_guids.add(vdisk.guid)
        for vdisk in VDiskList.get_without_vmachine():
            if vdisk.info['object_type'] == 'BASE':
                vdisk_guids.add(vdisk.guid)

        logger.info('Gather Scrub - Checking {0} volumes for scrub work'.format(len(vdisk_guids)))
        local_machineid = System.get_my_machine_id()
        local_storage_router = None
        local_scrub_location = None
        local_vdisks_to_scrub = []
        result_set = ResultSet([])
        storage_router_list = []

        for index, scrub_info in enumerate(scrub_locations.items()):
            start_index = index * len(vdisk_guids) / len(scrub_locations)
            end_index = (index + 1) * len(vdisk_guids) / len(scrub_locations)
            storage_router = scrub_info[0]
            vdisk_guids_to_scrub = list(vdisk_guids)[start_index:end_index]
            local = storage_router.machine_id == local_machineid
            logger.info('Gather Scrub - Storage Router {0:<15} ({1}) - Scrubbing {2} virtual disks'.format(storage_router.ip, 'local' if local is True else 'remote', len(vdisk_guids_to_scrub)))

            if local is True:
                local_storage_router = storage_router
                local_scrub_location = scrub_info[1]
                local_vdisks_to_scrub = vdisk_guids_to_scrub
            else:
                result_set.add(ScheduledTaskController._execute_scrub_work.s(scrub_location=scrub_info[1],
                                                                             vdisk_guids=vdisk_guids_to_scrub).apply_async(
                    routing_key='sr.{0}'.format(storage_router.machine_id)
                ))
                storage_router_list.append(storage_router)

        # Remote tasks have been launched, now start the local task and then wait for remote tasks to finish
        processed_guids = []
        if local_scrub_location is not None and len(local_vdisks_to_scrub) > 0:
            try:
                processed_guids = ScheduledTaskController._execute_scrub_work(scrub_location=local_scrub_location,
                                                                              vdisk_guids=local_vdisks_to_scrub)
            except Exception as ex:
                logger.error('Gather Scrub - Storage Router {0:<15} - Scrubbing failed with error:\n - {1}'.format(local_storage_router.ip, ex))
        all_results = result_set.join(propagate=False)  # Propagate False makes sure all jobs are waited for even when 1 or more jobs fail
        for index, result in enumerate(all_results):
            if isinstance(result, list):
                processed_guids.extend(result)
            else:
                logger.error('Gather Scrub - Storage Router {0:<15} - Scrubbing failed with error:\n - {1}'.format(storage_router_list[index].ip, result))
        if len(processed_guids) != len(vdisk_guids) or set(processed_guids).difference(vdisk_guids):
            raise RuntimeError('Scrubbing failed for 1 or more storagerouters')
        logger.info('Gather Scrub - Finished')

    @staticmethod
    @celery.task(name='ovs.scheduled.execute_scrub_work')
    def _execute_scrub_work(scrub_location, vdisk_guids):
        def verify_mds_config(current_vdisk):
            """
            Retrieve the metadata backend configuration for vDisk
            :param current_vdisk: vDisk to retrieve configuration for
            :type current_vdisk:  vDisk

            :return:              MDS configuration for vDisk
            """
            current_vdisk.invalidate_dynamics(['info'])
            vdisk_configs = current_vdisk.info['metadata_backend_config']
            if len(vdisk_configs) == 0:
                raise RuntimeError('Could not load MDS configuration')
            return vdisk_configs

        logger.info('Execute Scrub - Started')
        logger.info('Execute Scrub - Scrub location - {0}'.format(scrub_location))
        total = len(vdisk_guids)
        skipped = 0
        storagedrivers = {}
        failures = []
        for vdisk_guid in vdisk_guids:
            vdisk = VDisk(vdisk_guid)
            try:
                # Load the vDisk's StorageDriver
                logger.info('Execute Scrub - Virtual disk {0} - {1} - Started'.format(vdisk.guid, vdisk.name))
                vdisk.invalidate_dynamics(['storagedriver_id'])
                if vdisk.storagedriver_id not in storagedrivers:
                    storagedrivers[vdisk.storagedriver_id] = StorageDriverList.get_by_storagedriver_id(vdisk.storagedriver_id)
                storagedriver = storagedrivers[vdisk.storagedriver_id]

                # Load the vDisk's MDS configuration
                configs = verify_mds_config(current_vdisk=vdisk)

                # Check MDS master is local. Trigger MDS handover if necessary
                if configs[0].get('ip') != storagedriver.storagerouter.ip:
                    logger.debug('Execute Scrub - Virtual disk {0} - {1} - MDS master is not local, trigger handover'.format(vdisk.guid, vdisk.name))
                    MDSServiceController.ensure_safety(vdisk)
                    configs = verify_mds_config(current_vdisk=vdisk)
                    if configs[0].get('ip') != storagedriver.storagerouter.ip:
                        skipped += 1
                        logger.info('Execute Scrub - Virtual disk {0} - {1} - Skipping because master MDS still not local'.format(vdisk.guid, vdisk.name))
                        continue
                with vdisk.storagedriver_client.make_locked_client(str(vdisk.volume_id)) as locked_client:
                    logger.info('Execute Scrub - Virtual disk {0} - {1} - Retrieve and apply scrub work'.format(vdisk.guid, vdisk.name))
                    work_units = locked_client.get_scrubbing_workunits()
                    for work_unit in work_units:
                        scrubbing_result = locked_client.scrub(work_unit, scrub_location, logfile=SCRUBBER_LOGFILE_LOCATION)
                        locked_client.apply_scrubbing_result(scrubbing_result)
                    if work_units:
                        logger.info('Execute Scrub - Virtual disk {0} - {1} - Scrub successfully applied'.format(vdisk.guid, vdisk.name))
                    else:
                        logger.info('Execute Scrub - Virtual disk {0} - {1} - No scrubbing required'.format(vdisk.guid, vdisk.name))
            except Exception as ex:
                failures.append('Failed scrubbing work unit for volume {0} with guid {1}: {2}'.format(vdisk.name, vdisk.guid, ex))

        failed = len(failures)
        logger.info('Execute Scrub - Finished - Success: {0} - Failed: {1} - Skipped: {2}'.format((total - failed - skipped), failed, skipped))
        if failed > 0:
            raise Exception('\n - '.join(failures))
        return vdisk_guids

    @staticmethod
    @celery.task(name='ovs.scheduled.collapse_arakoon', schedule=crontab(minute='10', hour='0,2,4,6,8,10,12,14,16,18,20,22'))
    @ensure_single(task_name='ovs.scheduled.collapse_arakoon')
    def collapse_arakoon():
        """
        Collapse Arakoon's Tlogs
        :return: None
        """
        logger.info('Starting arakoon collapse')
        arakoon_clusters = {}
        for service in ServiceList.get_services():
            if service.type.name in ('Arakoon', 'NamespaceManager', 'AlbaManager'):
                arakoon_clusters[service.name.replace('arakoon-', '')] = service.storagerouter

        for cluster, storagerouter in arakoon_clusters.iteritems():
            logger.info('  Collapsing cluster {0}'.format(cluster))
            contents = EtcdConfiguration.get(ArakoonClusterConfig.ETCD_CONFIG_KEY.format(cluster), raw=True)
            parser = RawConfigParser()
            parser.readfp(StringIO(contents))
            nodes = {}
            for node in parser.get('global', 'cluster').split(','):
                node = node.strip()
                nodes[node] = ([parser.get(node, 'ip')], parser.get(node, 'client_port'))
            config = ArakoonClientConfig(str(cluster), nodes)
            for node in nodes.keys():
                logger.info('    Collapsing node: {0}'.format(node))
                client = ArakoonAdminClient(node, config)
                try:
                    client.collapse_tlogs(2)
                except:
                    logger.exception('Error during collapsing cluster {0} node {1}'.format(cluster, node))

        logger.info('Arakoon collapse finished')
