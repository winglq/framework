#!/usr/bin/env python2
# Copyright 2014 Open vStorage NV
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import re
from subprocess import check_output
from ovs.extensions.os.os import OSManager

SECRET_KEY_LENGTH = 50


def file_read(fn):
    with open(fn, 'r') as the_file:
        return the_file.read().strip()


def file_write(fn, cts):
    with open(fn, 'w') as the_file:
        the_file.write(cts)

# Disable/stop default services. Will be replaced by upstart scripts
run_level_regex = '^[KS][0-9]{2}(.*)'
for service_name in ('rabbitmq-server', 'memcached'):
    service_configured = False
    for run_level in range(7):
        for run_entry in os.listdir('/etc/rc{0}.d'.format(run_level)):
            if re.match(run_level_regex, run_entry) and service_name in run_entry:
                service_configured = True
                break

    if service_configured is True:
        check_output('service {0} stop'.format(service_name), shell=True)
        check_output('update-rc.d {0} disable'.format(service_name), shell=True)

# Cleanup *.pyc files
check_output('chown -R ovs:ovs /opt/OpenvStorage', shell=True)
check_output('find /opt/OpenvStorage -name *.pyc -exec rm -rf {} \;', shell=True)

# Few logstash cleanups
check_output('usermod -a -G adm logstash', shell=True)
if os.path.exists('/etc/init/logstash-web.conf'):
    check_output('echo manual > /etc/init/logstash-web.override', shell=True)

# Configure logging
check_output('chmod 755 /opt/OpenvStorage/scripts/system/rotate-storagedriver-logs.sh', shell=True)
if not os.path.exists('/etc/rsyslog.d/90-ovs.conf') or '$KLogPermitNonKernelFacility on' not in file_read('/etc/rsyslog.d/90-ovs.conf'):
    check_output('echo "\$KLogPermitNonKernelFacility on" > /etc/rsyslog.d/90-ovs.conf', shell=True)
    check_output('service rsyslog restart', shell=True)

# Add crontabs
cron_contents = check_output('crontab -l 2>/dev/null || true', shell=True).splitlines()
for cron_rule in ['0 * * * * /usr/sbin/ntpdate pool.ntp.org',
                  '* * * * * ovs monitor heartbeat',
                  '59 23 * * * /opt/OpenvStorage/scripts/system/rotate-storagedriver-logs.sh']:
    if cron_rule not in cron_contents:
        check_output('(crontab -l 2>/dev/null; echo "{0}") | crontab -'.format(cron_rule), shell=True)

# Creating configuration file
if not os.path.isfile('/opt/OpenvStorage/config/ovs.json'):
    check_output('cp /opt/OpenvStorage/config/templates/ovs.json /opt/OpenvStorage/config/ovs.json', shell=True)

# Configure SSH
config_file = '/etc/ssh/sshd_config'
ssh_content_before = None
if os.path.isfile(config_file):
    ssh_content_before = file_read(config_file)
    use_dns = False
    new_contents = []
    for line in file_read(config_file).splitlines():
        if 'AcceptEnv' in line:
            new_contents.append('#{0}'.format(line.strip().strip('#').strip()))
        elif 'UseDNS' in line:
            new_contents.append('UseDNS no')
            use_dns = True
        elif line.strip().startswith('Match'):
            if use_dns is False:
                new_contents.append('UseDNS no')
                use_dns = True
            new_contents.append(line)
        else:
            new_contents.append(line)
    if use_dns is False:
        new_contents.append('UseDNS no')
    file_write(config_file, '{0}\n'.format('\n'.join(new_contents)))
ssh_content_after = file_read(config_file)
if ssh_content_after != ssh_content_before:
    ssh_service = OSManager.get_ssh_service_name()
    check_output('service {0} restart'.format(ssh_service), shell=True)

# Configure coredumps
limits_file = '/etc/security/limits.conf'
if os.path.isfile(limits_file):
    contents = file_read(limits_file)
    if not re.search('\s?root\s+soft\s+core\s+unlimited\s?', contents):
        contents += '\nroot soft core unlimited'
    if not re.search('\s?ovs\s+soft\s+core\s+unlimited\s?', contents):
        contents += '\novs soft core unlimited'
    file_write(limits_file, '{0}\n'.format(contents))

# Generate SSH keys
root_ssh_folder = '{0}/.ssh'.format(check_output('echo ~root', shell=True).strip())
ovs_ssh_folder = '{0}/.ssh'.format(check_output('echo ~ovs', shell=True).strip())
private_key_filename = '{0}/id_rsa'
authorized_keys_filename = '{0}/authorized_keys'
known_hosts_filename = '{0}/known_hosts'

check_output('su - ovs -c "mkdir -p {0}"'.format(ovs_ssh_folder), shell=True)
check_output('su - ovs -c "chmod 755 {0}"'.format(ovs_ssh_folder), shell=True)

# Generate keys for root
if not os.path.exists(private_key_filename.format(root_ssh_folder)):
    check_output("ssh-keygen -t rsa -b 4096 -f {0} -N ''".format(private_key_filename.format(root_ssh_folder)), shell=True)
# Generate keys for ovs
if not os.path.exists(private_key_filename.format(ovs_ssh_folder)):
    check_output('su - ovs -c "ssh-keygen -t rsa -b 4096 -f {0} -N \'\'"'.format(private_key_filename.format(ovs_ssh_folder)), shell=True)

root_authorized_keys = authorized_keys_filename.format(root_ssh_folder)
ovs_authorized_keys = authorized_keys_filename.format(ovs_ssh_folder)
root_known_hosts = known_hosts_filename.format(root_ssh_folder)
ovs_known_hosts = known_hosts_filename.format(ovs_ssh_folder)

for filename in [root_authorized_keys, root_known_hosts]:
    check_output('touch {0}'.format(filename), shell=True)
    check_output('chmod 600 {0}'.format(filename), shell=True)

for filename in [ovs_authorized_keys, ovs_known_hosts]:
    check_output('su - ovs -c "touch {0}"'.format(filename), shell=True)
    check_output('su - ovs -c "chmod 600 {0}"'.format(filename), shell=True)
