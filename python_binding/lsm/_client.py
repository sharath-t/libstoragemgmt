# Copyright (C) 2011-2014 Red Hat, Inc.
# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2.1 of the License, or any later version.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this library; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301 USA
#
# Author: tasleson

import time
import os
import unittest
from lsm import (Volume, NfsExport, Capabilities, Pool, System,
                 Initiator, Disk, AccessGroup, FileSystem, FsSnapshot,
                 uri_parse, LsmError, JobStatus, ErrorNumber,
                 INetworkAttachedStorage, NfsExport)

from _common import return_requires as _return_requires
from _common import UDS_PATH as _UDS_PATH
from _transport import TransPort as _TransPort
from _data import IData as _IData


## Removes self for the hash d
# @param    d   Hash to remove self from
# @returns d with hash removed.
def _del_self(d):
    """
    Used to remove the self key from the dict d.  Self is included when calling
    the function locals() in a class method.
    """
    del d['self']
    return d


def _check_search_key(search_key, supported_keys):
    if search_key and search_key not in supported_keys:
        raise LsmError(ErrorNumber.UNSUPPORTED_SEARCH_KEY,
                       "Unsupported search_key: '%s'" % search_key)
    return


## Descriptive exception about daemon not running.
def _raise_no_daemon():
    raise LsmError(ErrorNumber.DAEMON_NOT_RUNNING,
                   "The libStorageMgmt daemon is not running (process "
                   "name lsmd), try 'service libstoragemgmt start'")


## Main client class for library.
# ** IMPORTANT **
# Theory of operation for methods in this class.
# We are using the name of the method and the name of the parameters and
# using python introspection abilities to translate them to the method and
# parameter names.  Makes the code compact, but you will break things if the
# IPlugin class does not match the method names and parameters here!
class Client(INetworkAttachedStorage):
    """
    Client side class used for managing storage that utilises RPC mechanism.
    """
    ## Method added so that the interface for the client RPC and the plug-in
    ## itself match.
    def plugin_register(self, uri, plain_text_password, timeout_ms, flags=0):
        raise RuntimeError("Do not call directly!")

    ## Called when we are ready to initialize the plug-in.
    # @param    self                    The this pointer
    # @param    uri                     The uniform resource identifier
    # @param    plain_text_password     Password as plain text
    # @param    timeout_ms              The timeout in ms
    # @param    flags                   Reserved for future use, must be zero.
    # @returns None
    def __start(self, uri, password, timeout, flags=0):
        """
        Instruct the plug-in to get ready
        """
        self._tp.rpc('plugin_register', _del_self(locals()))

    ## Checks to see if any unix domain sockets exist in the base directory
    # and opens a socket to one to see if the server is actually there.
    # @param    self    The this pointer
    # @returns True if daemon appears to be present, else false.
    @staticmethod
    def _check_daemon_exists():
        uds_path = Client._plugin_uds_path()
        if os.path.exists(uds_path):
            for root, sub_folders, files in os.walk(uds_path):
                for filename in files:
                    uds = os.path.join(root, filename)

                    try:
                        #This operation will work if the daemon is available
                        s = _TransPort.get_socket(uds)
                        s.close()
                        return True
                    except LsmError:
                        pass
        else:
            #Base directory is not present?
            pass
        return False

    @staticmethod
    def _plugin_uds_path():
        rc = _UDS_PATH

        if 'LSM_UDS_PATH' in os.environ:
            rc = os.environ['LSM_UDS_PATH']

        return rc

    ## Class constructor
    # @param    self                    The this pointer
    # @param    uri                     The uniform resource identifier
    # @param    plain_text_password     Password as plain text (Optional)
    # @param    timeout_ms              The timeout in ms
    # @param    flags                   Reserved for future use, must be zero.
    # @returns None
    def __init__(self, uri, plain_text_password=None, timeout_ms=30000,
                 flags=0):
        self._uri = uri
        self._password = plain_text_password
        self._timeout = timeout_ms
        self._uds_path = Client._plugin_uds_path()

        u = uri_parse(uri, ['scheme'])

        scheme = u['scheme']
        if "+" in scheme:
            (plug, proto) = scheme.split("+")
            scheme = plug

        self.plugin_path = os.path.join(self._uds_path, scheme)

        if os.path.exists(self.plugin_path):
            self._tp = _TransPort(_TransPort.get_socket(self.plugin_path))
        else:
            #At this point we don't know if the user specified an incorrect
            #plug-in in the URI or the daemon isn't started.  We will check
            #the directory for other unix domain sockets.
            if Client._check_daemon_exists():
                raise LsmError(ErrorNumber.PLUGIN_NOT_EXIST,
                               "Plug-in %s not found!" % self.plugin_path)
            else:
                _raise_no_daemon()

        self.__start(uri, plain_text_password, timeout_ms, flags)

    ## Synonym for close.
    @_return_requires(None)
    def plugin_unregister(self, flags=0):
        """
        Synonym for close.
        """
        self.close(flags)

    ## Does an orderly plugin_unregister of the plug-in
    # @param    self    The this pointer
    # @param    flags   Reserved for future use, must be zero.
    @_return_requires(None)
    def close(self, flags=0):
        """
        Does an orderly plugin_unregister of the plug-in
        """
        self._tp.rpc('plugin_unregister', _del_self(locals()))
        self._tp.close()
        self._tp = None

    ## Retrieves all the available plug-ins
    @staticmethod
    @_return_requires([unicode])
    def available_plugins(field_sep=':', flags=0):
        """
        Retrieves all the available plug-ins

        Return list of strings of available plug-ins with the
        "desc<sep>version"
        """
        rc = []

        if not Client._check_daemon_exists():
            _raise_no_daemon()

        uds_path = Client._plugin_uds_path()

        for root, sub_folders, files in os.walk(uds_path):
            for filename in files:
                uds = os.path.join(root, filename)
                tp = _TransPort(_TransPort.get_socket(uds))
                i, v = tp.rpc('plugin_info', dict(flags=0))
                rc.append("%s%s%s" % (i, field_sep, v))
                tp.close()

        return rc

    ## Sets the timeout for the plug-in
    # @param    self    The this pointer
    # @param    ms      Time-out in ms
    # @param    flags   Reserved for future use, must be zero.
    @_return_requires(None)
    def time_out_set(self, ms, flags=0):
        """
        Sets any time-outs for the plug-in (ms)

        Return None on success, else LsmError exception
        """
        return self._tp.rpc('time_out_set', _del_self(locals()))

    ## Retrieves the current time-out value.
    # @param    self    The this pointer
    # @param    flags   Reserved for future use, must be zero.
    # @returns  Time-out value
    @_return_requires(int)
    def time_out_get(self, flags=0):
        """
        Retrieves the current time-out

        Return time-out in ms, else raise LsmError
        """
        return self._tp.rpc('time_out_get', _del_self(locals()))

    ## Retrieves the status of the specified job id.
    # @param    self    The this pointer
    # @param    job_id  The job identifier
    # @param    flags   Reserved for future use, must be zero.
    # @returns A tuple ( status (enumeration), percent_complete,
    # completed item)
    @_return_requires(int, int, _IData)
    def job_status(self, job_id, flags=0):
        """
        Returns the stats of the given job.

        Returns a tuple ( status (enumeration), percent_complete,
                            completed item).
        else LsmError exception.
        """
        return self._tp.rpc('job_status', _del_self(locals()))

    ## Frees the resources for the specified job id.
    # @param    self    The this pointer
    # @param    job_id  Job id in which to release resource for
    # @param    flags   Reserved for future use, must be zero.
    @_return_requires(None)
    def job_free(self, job_id, flags=0):
        """
        Frees resources for a given job number.

        Returns None on success, else raises an LsmError
        """
        return self._tp.rpc('job_free', _del_self(locals()))

    ## Gets the capabilities of the array.
    # @param    self    The this pointer
    # @param    system  The system of interest
    # @param    flags   Reserved for future use, must be zero.
    # @returns  Capability object
    @_return_requires(Capabilities)
    def capabilities(self, system, flags=0):
        """
        Fetches the capabilities of the array

        Returns a capability object, see data,py for details.
        """
        return self._tp.rpc('capabilities', _del_self(locals()))

    ## Gets information about the plug-in
    # @param    self    The this pointer
    # @param    flags   Reserved for future use
    # @returns  Tuple (description, version)
    @_return_requires(unicode, unicode)
    def plugin_info(self, flags=0):
        """
        Returns a description and version of plug-in
        """
        return self._tp.rpc('plugin_info', _del_self(locals()))

    ## Returns an array of pool objects.
    # @param    self    The this pointer
    # @param    flags   When equal to Pool.RETRIEVE_FULL_INFO,
    #                   returned objects will contain optional data.
    #                   If not defined, only the mandatory properties will
    #                   returned.
    # @returns An array of pool objects.
    @_return_requires([Pool])
    def pools(self, search_key=None, search_value=None, flags=0):
        """
        Returns an array of pool objects.  Pools are used in both block and
        file system interfaces, thus the reason they are in the base class.
        """
        _check_search_key(search_key, Pool.SUPPORTED_SEARCH_KEYS)
        return self._tp.rpc('pools', _del_self(locals()))

    ## Create new pool in user friendly way. Depending on this capability:
    ##      Capabilities.POOL_CREATE
    ## For plugin developer: this method require complex codes to chose
    ## pool members, please refer to SimData.pool_create() in simarray.py for
    ## sample codes.
    ## Return the newly created pool object.
    # @param    self        The this pointer
    # @param    system      The system where new pool should reside.
    # @param    pool_name   The name for new pool. Will not fail if created
    #                       pool_name is not the same as requested.
    # @param    size_bytes  The size in bytes for new pool.
    #                       New pool can have equal or larger size than
    #                       requested, but not less. Should larger than 0.
    # @param    raid_type   Optional. If defined, new pool should using
    #                       defined RAID type.
    #                       When member_type was set to Pool.MEMBER_TYPE_POOL,
    #                       only allowed raid_type is RAID_TYPE_UNKNOWN or
    #                       RAID_TYPE_NOT_APPLICABLE
    # @param    member_type Optional. If defined, new pool will be assembled
    #                       by defined member types. For example;
    #                       when member_type == Pool.MEMBER_TYPE_DISK_SAS,
    #                       new pool will be created from SAS disks only.
    # @param    flags       Reserved for future use.
    # @returns  A tuple (job_id, new_pool), when one is None the other is
    #           valid.
    @_return_requires(unicode, Pool)
    def pool_create(self, system, pool_name, size_bytes,
                    raid_type=Pool.RAID_TYPE_UNKNOWN,
                    member_type=Pool.MEMBER_TYPE_UNKNOWN, flags=0):
        """
        Returns the created new pool object.
        """
        if size_bytes <= 0:
            raise LsmError(ErrorNumber.INVALID_ARGUMENT,
                           "'size_bytes' should larger than 0")
        return self._tp.rpc('pool_create', _del_self(locals()))

    ## Create new pool in the hard way by defined what exactly disks should
    ## be used. Depending on these capabilities:
    ##      Capabilities.POOL_CREATE_FROM_DISKS
    ## Return the newly created pool object with all supported optional data.
    # @param    self        The this pointer
    # @param    system_id   The id of system where new pool should reside.
    # @param    pool_name   The name for new pool. Will not fail if created
    #                       pool_name is not the same as requested.
    # @param    Disks       The disks (list) to create new pool from.
    #                       The new pool could contain more disks than
    #                       requested due to internal needs, but if possible,
    #                       new pool should only contain requested disks.
    # @param    raid_type   The RAID level for new pool.
    #                       Capabilities.POOL_CREATE_DISK_RAID_XXX will
    #                       indicate the supported RAID level.
    # @param    flags       Reserved for future use.
    # @returns  A tuple (job_id, new_pool), when one is None the other is
    #           valid.
    @_return_requires(unicode, Pool)
    def pool_create_from_disks(self, system_id, pool_name, disks,
                               raid_type, flags=0):
        """
        Creates pool from disks.
        Returns the created new pool object.
        """
        return self._tp.rpc('pool_create_from_disks', _del_self(locals()))

    ## Create new pool in the hard way by defined what exactly volumes should
    ## be used. Depending on these capabilities:
    ##      Capabilities.POOL_CREATE_FROM_VOLUMES
    ## Return the newly created pool object with all supported optional data.
    # @param    self        The this pointer
    # @param    system_id   The id of system where new pool should reside.
    # @param    pool_name   The name for new pool. Will not fail if created
    #                       pool_name is not the same as requested.
    # @param    volumes     The volumes to create new pool from.
    #                       The new pool could contain more volumes than
    #                       requested due to internal needs, but if possible,
    #                       new pool should only contain requested volumes.
    # @param    raid_type   The RAID level for new pool.
    #                       Capabilities.POOL_CREATE_VOLUME_RAID_XXX will
    #                       indicate the supported RAID level.
    # @param    flags       Reserved for future use.
    # @returns  A tuple (job_id, new_pool), when one is None the other is
    #           valid.
    @_return_requires(unicode, Pool)
    def pool_create_from_volumes(self, system_id, pool_name, volumes,
                                 raid_type, flags=0):
        """
        Creates pool from volumes.
        Returns the created new pool object.
        """
        return self._tp.rpc('pool_create_from_volumes', _del_self(locals()))

    ## Create new pool in the hard way by defined what exactly pool should
    ## be allocate space from. Depending on this capability:
    ##      Capabilities.POOL_CREATE_FROM_POOL
    ## Return the newly created pool object with all supported optional data.
    # @param    self        The this pointer
    # @param    system_id   The id of system where new pool should reside.
    # @param    pool_name   The name for new pool. Will not fail if created
    #                       pool_name is not the same as requested.
    # @param    pool        The pool to allocate space from for new pool.
    # @param    size_bytes  The size of the new pool.
    # @param    flags       Reserved for future use.
    # @returns  A tuple (job_id, new_pool), when one is None the other is
    #           valid.
    @_return_requires(unicode, Pool)
    def pool_create_from_pool(self, system_id, pool_name, pool,
                              size_bytes, flags=0):
        """
        Creates pool from volumes.
        Returns the created new pool object.
        """
        return self._tp.rpc('pool_create_from_pool', _del_self(locals()))

    ## Remove a pool. This method depend on Capabilities.POOL_DELETE
    # @param    self        The this pointer
    # @param    pool        The pool object
    # @param    flags       Reserved for future use, must be zero.
    # @returns None on success, else job id.  Raises LsmError on errors.
    @_return_requires(unicode)
    def pool_delete(self, pool, flags=0):
        """
        Return None on success, else job id. Raises LsmError on errors.
        """
        return self._tp.rpc('pool_delete', _del_self(locals()))

    ## Returns an array of system objects.
    # @param    self    The this pointer
    # @param    flags   Reserved for future use, must be zero.
    # @returns An array of system objects.
    @_return_requires([System])
    def systems(self, flags=0):
        """
        Returns an array of system objects.  System information is used to
        distinguish resources from on storage array to another when the plug=in
        supports the ability to have more than one array managed by it
        """
        return self._tp.rpc('systems', _del_self(locals()))

    ## Returns an array of initiator objects
    # @param    self    The this pointer
    # @param    flags   Reserved for future use, must be zero.
    # @returns An array of initiator objects.
    @_return_requires([Initiator])
    def initiators(self, flags=0):
        """
        Return an array of initiator objects
        """
        return self._tp.rpc('initiators', _del_self(locals()))

    ## Register a user/password for the specified initiator for CHAP
    #  authentication.
    # Note: If you pass an empty user and password the expected behavior is to
    #       remove any authentication for the specified initiator.
    # @param    self            The this pointer
    # @param    initiator       The initiator object
    # @param    in_user         User for inbound CHAP
    # @param    in_password     Password for inbound CHAP
    # @param    out_user        Outbound username
    # @param    out_password    Outbound password
    # @param    flags   Reserved for future use, must be zero.
    # @returns None on success, throws LsmError on errors.
    @_return_requires(None)
    def iscsi_chap_auth(self, initiator, in_user, in_password,
                        out_user, out_password, flags=0):
        """
        Register a user/password for the specified initiator for CHAP
        authentication.
        """
        return self._tp.rpc('iscsi_chap_auth', _del_self(locals()))

    ## Grants access so an initiator can read/write the specified volume.
    # @param    self            The this pointer
    # @param    initiator_id    The iqn, WWID etc.
    # @param    initiator_type  Enumerated initiator type
    # @param    volume          Volume to grant access to
    # @param    access          Enumerated access type
    # @param    flags           Reserved for future use, must be zero
    # @returns  None on success, throws LsmError on errors.
    @_return_requires(None)
    def initiator_grant(self, initiator_id, initiator_type, volume, access,
                        flags=0):
        """
        Allows an initiator to access a volume.
        """
        return self._tp.rpc('initiator_grant', _del_self(locals()))

    ## Revokes access for a volume for the specified initiator.
    # @param    self            The this pointer
    # @param    initiator       The iqn, WWID etc.
    # @param    volume          The volume to revoke access for
    # @param    flags           Reserved for future use, must be zero
    # @returns None on success, throws LsmError on errors.
    @_return_requires(None)
    def initiator_revoke(self, initiator, volume, flags=0):
        """
        Revokes access to a volume for the specified initiator
        """
        return self._tp.rpc('initiator_revoke', _del_self(locals()))

    ## Returns a list of volumes that are accessible from the specified
    # initiator.
    # @param    self            The this pointer
    # @param    initiator       The initiator object
    # @param    flags           Reserved for future use, must be zero
    @_return_requires([Volume])
    def volumes_accessible_by_initiator(self, initiator, flags=0):
        """
        Returns a list of volumes that the initiator has access to.
        """
        return self._tp.rpc('volumes_accessible_by_initiator',
                            _del_self(locals()))

    ## Returns a list of initiators that have access to the specified volume.
    # @param    self        The this pointer
    # @param    volume      The volume in question
    # @param    flags       Reserved for future use, must be zero
    # @returns  List of initiators
    @_return_requires([Initiator])
    def initiators_granted_to_volume(self, volume, flags=0):
        return self._tp.rpc('initiators_granted_to_volume',
                            _del_self(locals()))

    ## Returns an array of volume objects
    # @param    self    The this pointer
    # @param    flags   Reserved for future use, must be zero.
    # @returns An array of volume objects.
    @_return_requires([Volume])
    def volumes(self, search_key=None, search_value=None, flags=0):
        """
        Returns an array of volume objects
        """
        _check_search_key(search_key, Volume.SUPPORTED_SEARCH_KEYS)
        return self._tp.rpc('volumes', _del_self(locals()))

    ## Creates a volume
    # @param    self            The this pointer
    # @param    pool            The pool object to allocate storage from
    # @param    volume_name     The human text name for the volume
    # @param    size_bytes      Size of the volume in bytes
    # @param    provisioning    How the volume is to be provisioned
    # @param    flags           Reserved for future use, must be zero.
    # @returns  A tuple (job_id, new volume), when one is None the other is
    #           valid.
    @_return_requires(unicode, Volume)
    def volume_create(self, pool, volume_name, size_bytes, provisioning,
                      flags=0):
        """
        Creates a volume, given a pool, volume name, size and provisioning

        returns a tuple (job_id, new volume)
        Note: Tuple return values are mutually exclusive, when one
        is None the other must be valid.
        """
        return self._tp.rpc('volume_create', _del_self(locals()))

    ## Re-sizes a volume
    # @param    self    The this pointer
    # @param    volume  The volume object to re-size
    # @param    new_size_bytes  Size of the volume in bytes
    # @param    flags   Reserved for future use, must be zero.
    # @returns  A tuple (job_id, new re-sized volume), when one is
    #           None the other is valid.
    @_return_requires(unicode, Volume)
    def volume_resize(self, volume, new_size_bytes, flags=0):
        """
        Re-sizes a volume.

        Returns a tuple (job_id, re-sized_volume)
        Note: Tuple return values are mutually exclusive, when one
        is None the other must be valid.
        """
        return self._tp.rpc('volume_resize', _del_self(locals()))

    ## Replicates a volume from the specified pool.
    # @param    self        The this pointer
    # @param    pool        The pool to re-size from
    # @param    rep_type    Replication type
    #                       (enumeration,see common.data.Volume)
    # @param    volume_src  The volume to replicate
    # @param    name        Human readable name of replicated volume
    # @param    flags       Reserved for future use, must be zero.
    # @returns  A tuple (job_id, new replicated volume), when one is
    #           None the other is valid.
    @_return_requires(unicode, Volume)
    def volume_replicate(self, pool, rep_type, volume_src, name, flags=0):
        """
        Replicates a volume from the specified pool.

        Returns a tuple (job_id, replicated volume)
        Note: Tuple return values are mutually exclusive, when one
        is None the other must be valid.
        """
        return self._tp.rpc('volume_replicate', _del_self(locals()))

    ## Size of a replicated block.
    # @param    self    The this pointer
    # @param    system  The system to request the rep. block range size from
    # @param    flags   Reserved for future use, must be zero
    # @returns  Size of the replicated block in bytes
    @_return_requires(int)
    def volume_replicate_range_block_size(self, system, flags=0):
        """
        Returns the size of a replicated block in bytes.
        """
        return self._tp.rpc('volume_replicate_range_block_size',
                            _del_self(locals()))

    ## Replicates a portion of a volume to itself or another volume.
    # @param    self    The this pointer
    # @param    rep_type    Replication type
    #                       (enumeration, see common.data.Volume)
    # @param    volume_src  The volume src to replicate from
    # @param    volume_dest The volume dest to replicate to
    # @param    ranges      An array of Block range objects
    #                       @see lsm.common.data.BlockRange
    # @param    flags       Reserved for future use, must be zero.
    # @returns Job id or None when completed, else raises LsmError on errors.
    @_return_requires(unicode)
    def volume_replicate_range(self, rep_type, volume_src, volume_dest, ranges,
                               flags=0):
        """
        Replicates a portion of a volume to itself or another volume.  The src,
        dest and number of blocks values change with vendor, call
        volume_replicate_range_block_size to get block unit size.

        Returns Job id or None when completed, else raises LsmError on errors.
        """
        return self._tp.rpc('volume_replicate_range', _del_self(locals()))

    ## Deletes a volume
    # @param    self    The this pointer
    # @param    volume  The volume object which represents the volume to delete
    # @param    flags   Reserved for future use, must be zero.
    # @returns None on success, else job id.  Raises LsmError on errors.
    @_return_requires(unicode)
    def volume_delete(self, volume, flags=0):
        """
        Deletes a volume.

        Returns None on success, else job id
        """
        return self._tp.rpc('volume_delete', _del_self(locals()))

    ## Makes a volume online and available to the host.
    # @param    self    The this pointer
    # @param    volume  The volume to place online
    # @param    flags   Reserved for future use, must be zero.
    # @returns None on success, else raises LsmError
    @_return_requires(unicode)
    def volume_online(self, volume, flags=0):
        """
        Makes a volume available to the host

        returns None on success, else raises LsmError on errors.
        """
        return self._tp.rpc('volume_online', _del_self(locals()))

    ## Takes a volume offline
    # @param    self    The this pointer
    # @param    volume  The volume object
    # @param    flags   Reserved for future use, must be zero.
    # @returns None on success, else raises LsmError on errors.
    @_return_requires(None)
    def volume_offline(self, volume, flags=0):
        """
        Makes a volume unavailable to the host

        returns None on success, else raises LsmError on errors.
        """
        return self._tp.rpc('volume_offline', _del_self(locals()))

    ## Returns an array of disk objects
    # @param    self    The this pointer
    # @param    flags   When equal to DISK.RETRIEVE_FULL_INFO
    #                   returned objects will contain optional data.
    #                   If not defined, only the mandatory properties will
    #                   be returned.
    # @returns An array of disk objects.
    @_return_requires([Disk])
    def disks(self, search_key=None, search_value=None, flags=0):
        """
        Returns an array of disk objects
        """
        _check_search_key(search_key, Disk.SUPPORTED_SEARCH_KEYS)
        return self._tp.rpc('disks', _del_self(locals()))

    ## Access control for allowing an access group to access a volume
    # @param    self    The this pointer
    # @param    group   The access group
    # @param    volume  The volume to grant access to
    # @param    access  The desired access
    # @param    flags   Reserved for future use, must be zero.
    # @returns None on success, throws LsmError on errors.
    @_return_requires(None)
    def access_group_grant(self, group, volume, access, flags=0):
        """
        Allows an access group to access a volume.
        """
        return self._tp.rpc('access_group_grant', _del_self(locals()))

    ## Revokes access to a volume to initiators in an access group
    # @param    self    The this pointer
    # @param    group   The access group
    # @param    volume  The volume to grant access to
    # @param    flags   Reserved for future use, must be zero.
    # @returns None on success, throws LsmError on errors.
    @_return_requires(None)
    def access_group_revoke(self, group, volume, flags=0):
        """
        Revokes access for an access group for a volume
        """
        return self._tp.rpc('access_group_revoke', _del_self(locals()))

    ## Returns a list of access group objects
    # @param    self    The this pointer
    # @param    flags   Reserved for future use, must be zero.
    # @returns  List of access groups
    @_return_requires([AccessGroup])
    def access_groups(self, search_key=None, search_value=None, flags=0):
        """
        Returns a list of access groups
        """
        _check_search_key(search_key, AccessGroup.SUPPORTED_SEARCH_KEYS)
        return self._tp.rpc('access_groups', _del_self(locals()))

    ## Creates an access a group with the specified initiator in it.
    # @param    self                The this pointer
    # @param    name                The initiator group name
    # @param    initiator_id        Initiator id
    # @param    id_type             Type of initiator (Enumeration)
    # @param    system_id           Which system to create this group on
    # @param    flags               Reserved for future use, must be zero.
    # @returns AccessGroup on success, else raises LsmError
    @_return_requires(AccessGroup)
    def access_group_create(self, name, initiator_id, id_type, system_id,
                            flags=0):
        """
        Creates an access group and add the specified initiator id, id_type and
        desired access.
        """
        return self._tp.rpc('access_group_create', _del_self(locals()))

    ## Deletes an access group.
    # @param    self    The this pointer
    # @param    group   The access group to delete
    # @param    flags   Reserved for future use, must be zero.
    # @returns None on success, throws LsmError on errors.
    @_return_requires(None)
    def access_group_delete(self, group, flags=0):
        """
        Deletes an access group
        """
        return self._tp.rpc('access_group_delete', _del_self(locals()))

    ## Adds an initiator to an access group
    # @param    self            The this pointer
    # @param    group           Group to add initiator to
    # @param    initiator_id    Initiators id
    # @param    id_type         Initiator id type (enumeration)
    # @param    flags           Reserved for future use, must be zero.
    # @returns None on success, throws LsmError on errors.
    @_return_requires(None)
    def access_group_initiator_add(self, group, initiator_id, id_type,
                                   flags=0):
        """
        Adds an initiator to an access group
        """
        return self._tp.rpc('access_group_initiator_add', _del_self(locals()))

    ## Deletes an initiator from an access group
    # @param    self            The this pointer
    # @param    group           The access group to remove initiator from
    # @param    initiator_id    The initiator to remove from the group
    # @param    flags           Reserved for future use, must be zero.
    # @returns None on success, throws LsmError on errors.
    @_return_requires(None)
    def access_group_initiator_delete(self, group, initiator_id, flags=0):
        """
        Deletes an initiator from an access group
        """
        return self._tp.rpc('access_group_initiator_delete',
                            _del_self(locals()))

    ## Returns the list of volumes that access group has access to.
    # @param    self        The this pointer
    # @param    group       The access group to list volumes for
    # @param    flags       Reserved for future use, must be zero.
    # @returns list of volumes
    @_return_requires([Volume])
    def volumes_accessible_by_access_group(self, group, flags=0):
        """
        Returns the list of volumes that access group has access to.
        """
        return self._tp.rpc('volumes_accessible_by_access_group',
                            _del_self(locals()))

    ##Returns the list of access groups that have access to the specified
    #volume.
    # @param    self        The this pointer
    # @param    volume      The volume to list access groups for
    # @param    flags       Reserved for future use, must be zero.
    # @returns  list of access groups
    @_return_requires([AccessGroup])
    def access_groups_granted_to_volume(self, volume, flags=0):
        """
        Returns the list of access groups that have access to the specified
        volume.
        """
        return self._tp.rpc('access_groups_granted_to_volume',
                            _del_self(locals()))

    ## Checks to see if a volume has child dependencies.
    # @param    self    The this pointer
    # @param    volume  The volume to check
    # @param    flags   Reserved for future use, must be zero.
    # @returns True or False
    @_return_requires(bool)
    def volume_child_dependency(self, volume, flags=0):
        """
        Returns True if this volume has other volumes which are dependant on
        it. Implies that this volume cannot be deleted or possibly modified
        because it would affect its children.
        """
        return self._tp.rpc('volume_child_dependency', _del_self(locals()))

    ## Removes any child dependency.
    # @param    self    The this pointer
    # @param    volume  The volume to remove dependencies for
    # @param    flags   Reserved for future use, must be zero.
    # @returns None if complete, else job id.
    @_return_requires(unicode)
    def volume_child_dependency_rm(self, volume, flags=0):
        """
        If this volume has child dependency, this method call will fully
        replicate the blocks removing the relationship between them.  This
        should return None (success) if volume_child_dependency would return
        False.

        Note:  This operation could take a very long time depending on the size
        of the volume and the number of child dependencies.

        Returns None if complete else job id, raises LsmError on errors.
        """
        return self._tp.rpc('volume_child_dependency_rm', _del_self(locals()))

    ## Returns a list of file system objects.
    # @param    self    The this pointer
    # @param    flags   Reserved for future use, must be zero.
    # @returns A list of FS objects.
    @_return_requires([FileSystem])
    def fs(self, search_key=None, search_value=None, flags=0):
        """
        Returns a list of file systems on the controller.
        """
        _check_search_key(search_key, FileSystem.SUPPORTED_SEARCH_KEYS)
        return self._tp.rpc('fs', _del_self(locals()))

    ## Deletes a file system
    # @param    self    The this pointer
    # @param    fs      The file system to delete
    # @param    flags   Reserved for future use, must be zero.
    # @returns  None on success, else job id
    @_return_requires(unicode)
    def fs_delete(self, fs, flags=0):
        """
        WARNING: Destructive

        Deletes a file system and everything it contains
        Returns None on success, else job id
        """
        return self._tp.rpc('fs_delete', _del_self(locals()))

    ## Re-sizes a file system
    # @param    self            The this pointer
    # @param    fs              The file system to re-size
    # @param    new_size_bytes  The new size of the file system in bytes
    # @param    flags           Reserved for future use, must be zero.
    # @returns tuple (job_id, re-sized file system),
    # When one is None the other is valid
    @_return_requires(unicode, FileSystem)
    def fs_resize(self, fs, new_size_bytes, flags=0):
        """
        Re-size a file system

        Returns a tuple (job_id, re-sized file system)
        Note: Tuple return values are mutually exclusive, when one
        is None the other must be valid.
        """
        return self._tp.rpc('fs_resize', _del_self(locals()))

    ## Creates a file system.
    # @param    self        The this pointer
    # @param    pool        The pool object to allocate space from
    # @param    name        The human text name for the file system
    # @param    size_bytes  The size of the file system in bytes
    # @param    flags       Reserved for future use, must be zero.
    # @returns  tuple (job_id, file system),
    # When one is None the other is valid
    @_return_requires(unicode, FileSystem)
    def fs_create(self, pool, name, size_bytes, flags=0):
        """
        Creates a file system given a pool, name and size.
        Note: size is limited to 2**64 bytes

        Returns a tuple (job_id, file system)
        Note: Tuple return values are mutually exclusive, when one
        is None the other must be valid.
        """
        return self._tp.rpc('fs_create', _del_self(locals()))

    ## Clones a file system
    # @param    self            The this pointer
    # @param    src_fs          The source file system to clone
    # @param    dest_fs_name    The destination file system clone name
    # @param    snapshot        Optional, create clone from previous snapshot
    # @param    flags           Reserved for future use, must be zero.
    # @returns tuple (job_id, file system)
    @_return_requires(unicode, FileSystem)
    def fs_clone(self, src_fs, dest_fs_name, snapshot=None, flags=0):
        """
        Creates a thin, point in time read/writable copy of src to dest.
        Optionally uses snapshot as backing of src_fs

        Returns a tuple (job_id, file system)
        Note: Tuple return values are mutually exclusive, when one
        is None the other must be valid.
        """
        return self._tp.rpc('fs_clone', _del_self(locals()))

    ## Clones an individual file or files on the specified file system
    # @param    self            The this pointer
    # @param    fs              The file system the files are on
    # @param    src_file_name   The source file name
    # @param    dest_file_name  The dest. file name
    # @param    snapshot        Optional, the snapshot to base clone source
    #                                       file from
    # @param    flags           Reserved for future use, must be zero.
    # @returns  None on success, else job id
    @_return_requires(unicode)
    def fs_file_clone(self, fs, src_file_name, dest_file_name, snapshot=None,
                      flags=0):
        """
        Creates a thinly provisioned clone of src to dest.
        Note: Source and Destination are required to be on same filesystem and
        all directories in destination path need to exist.

        Returns None on success, else job id
        """
        return self._tp.rpc('fs_file_clone', _del_self(locals()))

    ## Returns a list of snapshots
    # @param    self    The this pointer
    # @param    fs      The file system
    # @param    flags   Reserved for future use, must be zero.
    # @returns  a list of snapshot objects.
    @_return_requires([FsSnapshot])
    def fs_snapshots(self, fs, flags=0):
        """
        Returns a list of snapshot names for the supplied file system
        """
        return self._tp.rpc('fs_snapshots', _del_self(locals()))

    ## Creates a snapshot (Point in time read only copy)
    # @param    self            The this pointer
    # @param    fs              The file system to snapshot
    # @param    snapshot_name   The human readable snapshot name
    # @param    files           The list of specific files to snapshot.
    # @param    flags           Reserved for future use, must be zero.
    # @returns tuple (job_id, snapshot)
    @_return_requires(unicode, FsSnapshot)
    def fs_snapshot_create(self, fs, snapshot_name, files, flags=0):
        """
        Snapshot is a point in time read-only copy

        Create a snapshot on the chosen file system with a supplied name for
        each of the files.  Passing None implies snapping all files on the file
        system.  When files is non-none it implies snap shooting those file.
        NOTE:  Some arrays only support snapshots at the file system level.  In
        this case it will not be considered an error if file names are passed.
        In these cases the file names are effectively discarded as all files
        are done.

        Returns a tuple (job_id, snapshot)
        Notes:
        - Snapshot name may not match what was passed in
          (depends on array implementation)
        - Tuple return values are mutually exclusive, when one
          is None the other must be valid.
        """
        return self._tp.rpc('fs_snapshot_create', _del_self(locals()))

    ## Deletes a snapshot
    # @param    self        The this pointer
    # @param    fs          The filesystem the snapshot it for
    # @param    snapshot    The specific snap shot to delete
    # @param    flags       Reserved for future use, must be zero.
    # @returns  None on success, else job id
    @_return_requires(unicode)
    def fs_snapshot_delete(self, fs, snapshot, flags=0):
        """
        Frees the re-sources for the given snapshot on the supplied filesystem.

        Returns None on success else job id, LsmError exception on error
        """
        return self._tp.rpc('fs_snapshot_delete', _del_self(locals()))

    ## Reverts a snapshot
    # @param    self            The this pointer
    # @param    fs              The file system object to restore snapshot for
    # @param    snapshot        The snapshot file to restore back too
    # @param    files           The specific files to restore
    # @param    restore_files   Individual files to restore
    # @param    all_files       Set to True if all files should be restored
    #                           back
    # @param    flags           Reserved for future use, must be zero.
    # @return None on success, else job id
    @_return_requires(unicode)
    def fs_snapshot_restore(self, fs, snapshot, files, restore_files,
                            all_files=False, flags=0):
        """
        WARNING: Destructive!

        Reverts a file-system or just the specified files from the snapshot.
        If a list of files is supplied but the array cannot restore just them
        then the operation will fail with an LsmError raised.  If files == None
        and all_files = True then all files on the file-system are restored.

        Restore_file if None none must be the same length as files with each
        index in each list referring to the associated file.

        Returns None on success, else job id, LsmError exception on error
        """
        return self._tp.rpc('fs_snapshot_restore', _del_self(locals()))

    ## Checks to see if a file system has child dependencies.
    # @param    fs      The file system to check
    # @param    files   The files to check (optional)
    # @param    flags   Reserved for future use, must be zero.
    # @returns True or False
    @_return_requires(bool)
    def fs_child_dependency(self, fs, files, flags=0):
        """
        Returns True if the specified filesystem or specified file on this
        file system has child dependencies.  This implies that this filesystem
        or specified file on this file system cannot be deleted or possibly
        modified because it would affect its children.
        """
        return self._tp.rpc('fs_child_dependency', _del_self(locals()))

    ## Removes child dependencies from a FS or specific file.
    # @param    self    The this pointer
    # @param    fs      The file system to remove child dependencies for
    # @param    files   The list of files to remove child dependencies (opt.)
    # @param    flags   Reserved for future use, must be zero.
    # @returns None if complete, else job id.
    @_return_requires(unicode)
    def fs_child_dependency_rm(self, fs, files, flags=0):
        """
        If this filesystem or specified file on this filesystem has child
        dependency this method will fully replicate the blocks removing the
        relationship between them.  This should return None(success) if
        fs_child_dependency would return False.

        Note:  This operation could take a very long time depending on the size
        of the filesystem and the number of child dependencies.

        Returns None if completed, else job id.  Raises LsmError on errors.
        """
        return self._tp.rpc('fs_child_dependency_rm', _del_self(locals()))

    ## Returns a list of all the NFS client authentication types.
    # @param    self    The this pointer
    # @param    flags   Reserved for future use, must be zero.
    # @returns  An array of client authentication types.
    @_return_requires([unicode])
    def export_auth(self, flags=0):
        """
        What types of NFS client authentication are supported.
        """
        return self._tp.rpc('export_auth', _del_self(locals()))

    ## Returns a list of all the exported file systems
    # @param    self    The this pointer
    # @param    flags   Reserved for future use, must be zero.
    # @returns An array of export objects
    @_return_requires([NfsExport])
    def exports(self, search_key=None, search_value=None, flags=0):
        """
        Get a list of all exported file systems on the controller.
        """
        _check_search_key(search_key, NfsExport.SUPPORTED_SEARCH_KEYS)
        return self._tp.rpc('exports', _del_self(locals()))

    ## Exports a FS as specified in the export.
    # @param    self            The this pointer
    # @param    fs_id           The FS ID to export
    # @param    export_path     The export path (Set to None for array to pick)
    # @param    root_list       List of hosts with root access
    # @param    rw_list         List of hosts with read/write access
    # @param    ro_list         List of hosts with read only access
    # @param    anon_uid        UID to map to anonymous
    # @param    anon_gid        GID to map to anonymous
    # @param    auth_type       NFS client authentication type
    # @param    options         Options to pass to plug-in
    # @param    flags           Reserved for future use, must be zero.
    # @returns NfsExport on success, else raises LsmError
    @_return_requires(NfsExport)
    def export_fs(self, fs_id, export_path, root_list, rw_list, ro_list,
                  anon_uid=NfsExport.ANON_UID_GID_NA,
                  anon_gid=NfsExport.ANON_UID_GID_NA,
                  auth_type=None, options=None, flags=0):
        """
        Exports a filesystem as specified in the arguments
        """
        return self._tp.rpc('export_fs', _del_self(locals()))

    ## Removes the specified export
    # @param    self    The this pointer
    # @param    export  The export to remove
    # @param    flags   Reserved for future use, must be zero.
    # @returns None on success, else raises LsmError
    @_return_requires(None)
    def export_remove(self, export, flags=0):
        """
        Removes the specified export
        """
        return self._tp.rpc('export_remove', _del_self(locals()))


class _TestClient(unittest.TestCase):
    def wait_to_finish(self, job, vol):

        if vol is not None:
            return vol
        else:
            (status, percent, volume) = self.c.job_status(job)
            print 'Job status:', status, ' percent complete=', percent

            while status == JobStatus.INPROGRESS:
                time.sleep(1)
                (status, percent, volume) = self.c.job_status(job)
                print 'Job status:', status, ' percent complete=', percent

            self.c.job_free(job)

            if status == JobStatus.COMPLETE:
                self.assertTrue(volume is not None)

        return volume

    def setUp(self):
        #Most of the uri is not needed for the simulator
        #Remember that the setup and teardown methods are run for each test
        #case!
        self.c = Client('sim://username@host:5988/?namespace=root/foo')

    def test_tmo(self):
        expected = 40000

        self.c.time_out_set(expected)
        tmo = self.c.time_out_get()
        self.assertTrue(tmo == expected)

    def test_job_errors(self):
        self.assertRaises(LsmError, self.c.job_free, 0)
        self.assertRaises(LsmError, self.c.job_status, 0)

    def test_pools(self):
        self.pools = self.c.pools()

        self.assertTrue(len(self.pools) == 4)

        for p in self.pools:
            print p

    def test_volumes(self):
        volumes = self.c.volumes()
        self.assertTrue(len(volumes) == 0)

        pools = self.c.pools()

        #create a volume
        p = pools[0]

        #Create volumes
        num_volumes = 10
        for i in range(num_volumes):
            vol = self.wait_to_finish(
                *(self.c.volume_create(p, "TestVol" + str(i), 1024 * 1024 * 10,
                                       Volume.PROVISION_DEFAULT)))
            print str(vol)

        volumes = self.c.volumes()
        self.assertTrue(len(volumes) == num_volumes)

        #delete volumes
        for i in volumes:
            self.c.volume_delete(i)

        volumes = self.c.volumes()
        self.assertTrue(len(volumes) == 0)

        #Create a volume and replicate it
        vol = self.wait_to_finish(
            *(self.c.volume_create(p, "To be replicated", 1024 * 1024 * 10,
                                   Volume.PROVISION_DEFAULT)))
        rep = self.wait_to_finish(
            *(self.c.volume_replicate(p, Volume.REPLICATE_CLONE, vol,
                                      'Replicated')))

        volumes = self.c.volumes()
        self.assertTrue(len(volumes) == 2)

        self.c.volume_delete(rep)

        re_sized = self.wait_to_finish(
            *(self.c.volume_resize(vol, vol.size_bytes * 2)))

        self.assertTrue(vol.size_bytes == re_sized.size_bytes / 2)

        self.c.volume_offline(re_sized)
        self.c.volume_online(re_sized)

    def tearDown(self):
        self.c.close()

if __name__ == "__main__":
    unittest.main()