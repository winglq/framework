﻿// Copyright (C) 2016 iNuron NV
//
// This file is part of Open vStorage Open Source Edition (OSE),
// as available from
//
//      http://www.openvstorage.org and
//      http://www.openvstorage.com.
//
// This file is free software; you can redistribute it and/or modify it
// under the terms of the GNU Affero General Public License v3 (GNU AGPLv3)
// as published by the Free Software Foundation, in version 3 as it comes
// in the LICENSE.txt file of the Open vStorage OSE distribution.
//
// Open vStorage is distributed in the hope that it will be useful,
// but WITHOUT ANY WARRANTY of any kind.
/*global define */
define([
    'jquery', 'durandal/app', 'plugins/dialog', 'knockout', 'plugins/router',
    'ovs/shared', 'ovs/generic', 'ovs/refresher', 'ovs/api',
    '../containers/vpool', '../containers/storagedriver', '../containers/storagerouter', '../containers/vdisk',
    '../wizards/addvpool/index'
], function($, app, dialog, ko, router,
            shared, generic, Refresher, api,
            VPool, StorageDriver, StorageRouter, VDisk,
            ExtendVPool) {
    "use strict";
    return function() {
        var self = this;

        // Variables
        self.shared             = shared;
        self.guard              = { authenticated: true };
        self.refresher          = new Refresher();
        self.widgets            = [];
        self.storageRouterCache = {};
        self.vDiskCache         = {};
        self.vDiskHeaders       = [
            { key: 'name',       value: $.t('ovs:generic.name'),       width: undefined },
            { key: 'size',       value: $.t('ovs:generic.size'),       width: 100       },
            { key: 'storedData', value: $.t('ovs:generic.storeddata'), width: 110       },
            { key: 'iops',       value: $.t('ovs:generic.iops'),       width: 90        },
            { key: 'readSpeed',  value: $.t('ovs:generic.read'),       width: 125       },
            { key: 'writeSpeed', value: $.t('ovs:generic.write'),      width: 125       },
            { key: 'dtlStatus',  value: $.t('ovs:generic.dtl_status'), width: 50        }
        ];
        self.vDiskQuery         = JSON.stringify({
            type: 'AND',
            items: [['is_vtemplate', 'EQUALS', false]]
        });

        // Handles
        self.vDisksHandle             = {};
        self.loadStorageDriversHandle = undefined;
        self.loadStorageRoutersHandle = undefined;

        // Observables
        self.updatingStorageRouters = ko.observable(false);
        self.vPool                  = ko.observable();
        self.srSDMap                = ko.observable({});
        self.storageDrivers         = ko.observableArray([]);
        self.storageRouters         = ko.observableArray([]);

        // Functions
        self.load = function() {
            return $.Deferred(function (deferred) {
                var vpool = self.vPool();
                $.when.apply($, [
                    vpool.load('storagedrivers,vdisks,_dynamics'),
                    self.loadStorageRouters(),
                    self.loadStorageDrivers()
                ])
                    .fail(function(error) {
                        if (error !== undefined && error.status === 404) {
                            router.navigate(shared.routing.loadHash('vpools'));
                        }
                    })
                    .always(deferred.resolve);
            }).promise();
        };
        self.loadStorageRouters = function() {
            return $.Deferred(function(deferred) {
                if (generic.xhrCompleted(self.loadStorageRoutersHandle)) {
                    var options = {
                        sort: 'name',
                        contents: 'storagedrivers'
                    };
                    self.loadStorageRoutersHandle = api.get('storagerouters', { queryparams: options })
                        .done(function(data) {
                            var guids = [], sadata = {};
                            $.each(data.data, function(index, item) {
                                guids.push(item.guid);
                                sadata[item.guid] = item;
                            });
                            generic.crossFiller(
                                guids, self.storageRouters,
                                function(guid) {
                                    return new StorageRouter(guid);
                                }, 'guid'
                            );
                            $.each(self.storageRouters(), function(index, storageRouter) {
                                if (sadata.hasOwnProperty(storageRouter.guid())) {
                                    storageRouter.fillData(sadata[storageRouter.guid()]);
                                }
                            });
                            deferred.resolve();
                        })
                        .fail(deferred.reject);
                } else {
                    deferred.resolve();
                }
            }).promise();
        };
        self.loadStorageDrivers = function() {
            return $.Deferred(function(deferred) {
                if (generic.xhrCompleted(self.loadStorageDriversHandle)) {
                    self.loadStorageDriversHandle = api.get('storagedrivers', {queryparams: {vpool_guid: self.vPool().guid(), contents: 'storagerouter,vpool_backend_info,vdisks_guids'}})
                        .done(function(data) {
                            var guids = [], sddata = {}, map = {};
                            $.each(data.data, function(index, item) {
                                guids.push(item.guid);
                                sddata[item.guid] = item;
                            });
                            generic.crossFiller(
                                guids, self.storageDrivers,
                                function(guid) {
                                    return new StorageDriver(guid);
                                }, 'guid'
                            );
                            $.each(self.storageDrivers(), function(index, storageDriver) {
                                if (sddata.hasOwnProperty(storageDriver.guid())) {
                                    storageDriver.fillData(sddata[storageDriver.guid()]);
                                }
                                map[storageDriver.storageRouterGuid()] = storageDriver;
                            });
                            self.srSDMap(map);
                            deferred.resolve();
                        })
                        .fail(function() {
                            deferred.reject();
                        });
                } else {
                    deferred.resolve();
                }
            }).promise();
        };
        self.loadVDisks = function(options) {
            return $.Deferred(function(deferred) {
                if (generic.xhrCompleted(self.vDisksHandle[options.page])) {
                    options.sort = 'devicename';
                    options.contents = '_dynamics,_relations,-snapshots';
                    options.vpoolguid = self.vPool().guid();
                    options.query = self.vDiskQuery;
                    self.vDisksHandle[options.page] = api.get('vdisks', { queryparams: options })
                        .done(function(data) {
                            deferred.resolve({
                                data: data,
                                loader: function(guid) {
                                    if (!self.vDiskCache.hasOwnProperty(guid)) {
                                        self.vDiskCache[guid] = new VDisk(guid);
                                    }
                                    return self.vDiskCache[guid];
                                }
                            });
                        })
                        .fail(function() { deferred.reject(); });
                } else {
                    deferred.resolve();
                }
            }).promise();
        };
        self.formatBytes = function(value) {
            return generic.formatBytes(value);
        };
        self.addStorageRouter = function(sr) {
            self.updatingStorageRouters(true);

            var deferred = $.Deferred(),
                wizard = new ExtendVPool({
                    modal: true,
                    completed: deferred,
                    vPool: self.vPool(),
                    storageRouter: sr
                });
            wizard.closing.always(function() {
                deferred.resolve();
            });
            dialog.show(wizard);
            deferred.always(function() {
                self.updatingStorageRouters(false);
            });
        };
        self.removeStorageRouter = function(sr) {
            var single = generic.keys(self.srSDMap()).length === 1;
            if (self.srSDMap().hasOwnProperty(sr.guid()) && self.srSDMap()[sr.guid()].canBeDeleted() === true) {
                self.updatingStorageRouters(true);
                app.showMessage(
                    $.t('ovs:wizards.shrink_vpool.confirm.remove_' + (single === true ? 'single' : 'multi'), { what: sr.name() }),
                    $.t('ovs:generic.areyousure'),
                    [$.t('ovs:generic.yes'), $.t('ovs:generic.no')]
                )
                    .done(function(answer) {
                        if (answer === $.t('ovs:generic.no')) {
                            self.updatingStorageRouters(false);
                        } else {
                            if (single === true) {
                                generic.alertInfo(
                                    $.t('ovs:wizards.shrink_vpool.confirm.started'),
                                    $.t('ovs:wizards.shrink_vpool.confirm.inprogress_single')
                                );
                            } else {
                                generic.alertInfo(
                                    $.t('ovs:wizards.shrink_vpool.confirm.started'),
                                    $.t('ovs:wizards.shrink_vpool.confirm.inprogress_multi')
                                );
                            }
                            api.post('vpools/' + self.vPool().guid() + '/shrink_vpool', { data: { storagerouter_guid: sr.guid() } })
                                .then(self.shared.tasks.wait)
                                .done(function() {
                                    if (single === true) {
                                        generic.alertSuccess(
                                            $.t('ovs:wizards.shrink_vpool.confirm.complete'),
                                            $.t('ovs:wizards.shrink_vpool.confirm.success_single')
                                        );
                                    } else {
                                        generic.alertSuccess(
                                            $.t('ovs:wizards.shrink_vpool.confirm.complete'),
                                            $.t('ovs:wizards.shrink_vpool.confirm.success_multi')
                                        );
                                    }
                                })
                                .fail(function() {
                                    if (single === true) {
                                        generic.alertError(
                                            $.t('ovs:generic.error'),
                                            $.t('ovs:wizards.shrink_vpool.confirm.failed_single')
                                        );
                                    } else {
                                        generic.alertError(
                                            $.t('ovs:generic.error'),
                                            $.t('ovs:wizards.shrink_vpool.confirm.failed_multi')
                                        );
                                    }
                                })
                                .always(function() {
                                    self.updatingStorageRouters(false);
                                });
                        }
                    });
            }
        };

        // Durandal
        self.activate = function(mode, guid) {
            self.vPool(new VPool(guid));
            self.refresher.init(self.load, 5000);
            self.refresher.run();
            self.refresher.start();
        };
        self.deactivate = function() {
            $.each(self.widgets, function(index, item) {
                item.deactivate();
            });
            self.refresher.stop();
        };
    };
});
