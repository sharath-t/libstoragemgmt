# Copyright (C) 2013 IBM Corporation
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
# Author: Deepak C Shetty (deepakcs@linux.vnet.ibm.com)

import paramiko

from lsm import (Capabilities, ErrorNumber, IStorageAreaNetwork, Initiator,
                 LsmError, Pool, System, VERSION, Volume, uri_parse,
                 search_property)


def handle_ssh_errors(method):
    def ssh_wrapper(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except paramiko.SSHException as sshe:
            raise LsmError(ErrorNumber.TRANSPORT_COMMUNICATION, str(sshe))

    return ssh_wrapper


class V7kError(Exception):
    """
    Class represents a v7k cli bad return code
    """

    def __init__(self, errno, reason, *args, **kwargs):
        Exception.__init__(self, *args, **kwargs)
        self.errno = errno
        self.reason = reason


# NOTE: v7k cli doc doesn't list the possible error codes per cli.
# Thus do very generic and basic error handling for now.
# Just pass the error msg & code returned by the array back to lsm.
def handle_v7k_errors(method):
    def v7k_wrapper(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except V7kError as ve:
            msg = ve.reason + " (vendor error code= " + str(ve.errno) + ")"
            raise LsmError(ErrorNumber.PLUGIN_ERROR, msg)

    return v7k_wrapper


# A lite weight sshclient
class SSHClient():

    @handle_ssh_errors
    def __init__(self, hostname, login, passwd, conn_timeout=30):
        self.ssh = paramiko.SSHClient()
        self.ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        try:
            self.ssh.connect(hostname, username=login, password=passwd,
                             timeout=conn_timeout)
        except Exception as e:
            msg = "Error while connecting via ssh to host %s : %s" % \
                  (hostname, e)
            raise paramiko.SSHException(msg)

    @handle_v7k_errors
    def execute(self, command, check_exit_code=True):
        stdin_strm, stdout_strm, stderr_strm = self.ssh.exec_command(command)

        stdin_strm.close()

        stdout_channel = stdout_strm.channel
        exit_code = stdout_channel.recv_exit_status()
        stdout_channel.close()

        stdout = stdout_strm.read()
        stdout_strm.close()

        stderr = stderr_strm.read()
        stderr_strm.close()

        if check_exit_code and exit_code != 0:
            errList = stderr.split(' ', 1)

            if exit_code == 127:
                # command not found error
                errno = exit_code
            else:
                # other errors
                errno = errList[0]

            reason = errList[1]

            raise V7kError(errno, reason)

        return exit_code, stdout, stderr

    @handle_ssh_errors
    def close(self):
        self.ssh.close()


# IBM V7000 storage lsm plugin

# *** lsm -- IBM V7000 terminology mapping ***
# volume -- v7k volume (aka vdisk)
# initiator -- host
# access group --  NA
# NA -- I/O group
# pool -- mdisk group
# volume-child dep -- ~fcmap and other maps if any
# job -- cli that can be invoked in async mode

class IbmV7k(IStorageAreaNetwork):

    def __init__(self):
        self.sys_info = None
        self.ssh = None
        self.tmo = 0
        self.password = None

    def _execute_command(self, ssh_cmd):
        exit_code, stdout, stderr = self.ssh.execute(ssh_cmd)
        return exit_code, stdout, stderr

    def _execute_command_and_parse_detailed(self, ssh_cmd):
        exit_code, stdout, stderr = self.ssh.execute(ssh_cmd)

        cmd_output = {}
        if not len(stdout.strip()):
            return cmd_output

        output_lines = stdout.split('\n')
        if not len(output_lines):
            return cmd_output

        for line in output_lines:
            name, foo, value = line.partition('!')
            if name is not None and len(name.strip()):
                cmd_output[name] = value

        return cmd_output

    def _execute_command_and_parse_concise(self, ssh_cmd):
        # This assume -nohdr is *not* present in ssh_cmd
        exit_code, stdout, stderr = self.ssh.execute(ssh_cmd)

        cmd_output = {}
        if not len(stdout.strip()):
            return cmd_output

        output_lines = stdout.split('\n')
        if not len(output_lines):
            return cmd_output

        header_line = output_lines[0]
        keylist = header_line.split('!')

        # For some reason, concise output gives one extra blank line at the end
        attrib_lines = output_lines[1:-1]
        lineindex = 0

        for attrib_line in attrib_lines:
            valuelist = attrib_line.split('!')
            attributes = {}
            valueindex = 0
            for key in keylist:
                attributes[key] = valuelist[valueindex]
                valueindex += 1

            cmd_output[lineindex] = attributes
            lineindex += 1

        return cmd_output

    def _get_system_info(self):
        ssh_cmd = 'lssystem -delim !'
        return self._execute_command_and_parse_detailed(ssh_cmd)

    def _get_pools(self):
        ssh_cmd = 'lsmdiskgrp -bytes -delim !'
        return self._execute_command_and_parse_concise(ssh_cmd)

    def _pool(self, p):
        return Pool(p['id'], p['name'], int(p['capacity']),
                    int(p['free_capacity']), Pool.STATUS_UNKNOWN, '',
                    self.sys_info.id)

    def _get_volumes(self):
        ssh_cmd = 'lsvdisk -bytes -delim !'
        return self._execute_command_and_parse_concise(ssh_cmd)

    def _get_volume(self, volume):
        ssh_cmd = 'lsvdisk -bytes -delim ! %s' % volume
        return self._execute_command_and_parse_detailed(ssh_cmd)

    def _volume(self, v):
        # v7k support 512 bytes/sec only, as the phy drive bs.
        # Its a bit complicated to reverse map v7k volume to
        # phy drive using the cli, so for now hardcode it as
        # thats the only supported bs at the drive level.
        bs = 512

        if v['status'] == 'online':
            vol_status = Volume.STATUS_OK
        elif v['status'] == 'offline':
            vol_status = Volume.STATUS_DORMANT
        else:
            vol_status = Volume.STATUS_ERR

        return Volume(v['id'], v['name'], v['vdisk_UID'], bs,
                      (float(v['capacity']) / bs), vol_status,
                      self.sys_info.id,
                      v['mdisk_grp_id'])

    def _create_volume(self, pool, vol_name, size_bytes, prov):
        ssh_cmd = ('mkvdisk -name %s -mdiskgrp %s -iogrp 0 -size %s'
                   ' -unit b') % (vol_name, pool, size_bytes)

        if prov == Volume.PROVISION_THIN:
            # Defaults for thinp
            rsize = 5
            warning = 0
            autoex = '-autoexpand'

            ssh_cmd += ' -rsize %d%% -warning %d %s' % \
                       (rsize, warning, autoex)

        exit_code, stdout, stderr = self.ssh.execute(ssh_cmd)

    def _delete_volume(self, volume, force):
        # NOTE: volume can be id or name
        ssh_cmd = 'rmvdisk %s %s' % ('-force' if force else '', volume)
        exit_code, stdout, stderr = self.ssh.execute(ssh_cmd)

    def _get_initiators(self):
        ssh_cmd = 'lshost -delim !'
        return self._execute_command_and_parse_concise(ssh_cmd)

    def _get_initiator(self, init):
        ssh_cmd = 'lshost -delim ! %s' % init
        return self._execute_command_and_parse_detailed(ssh_cmd)

    def _initiator(self, v7k_init):
        # NOTE: There is a terminology gap between what V7k thinks initiator id
        #       is and what lsm thinks initiator id is.
        #
        #       Class Initiator's 'id' field actually is the wwpn/iqn, but V7K
        #       only takes wwpn/iqn during mkhost, then assigns a numeric id &
        #       string name to a host, and all future reference to the host
        #       after mkhost is successfull, is using the numeric/string
        #       id/name only.

        if 'WWPN' in v7k_init:
            lsm_init_type = Initiator.TYPE_PORT_WWN
            # TODO: Add support for > 1 wwpn case.
            #       v7k cli is not parse friendly for > 1 case.
            lsm_init_id = v7k_init['WWPN']
        elif 'iscsi_name' in v7k_init:
            lsm_init_type = Initiator.TYPE_ISCSI
            # TODO: Add support for > 1 iscsiname case.
            #       v7k cli is not parse friendly for > 1 case.
            lsm_init_id = v7k_init['iscsi_name']
        else:
            # Since lshost worked, support it as other type.
            lsm_init_type = Initiator.TYPE_OTHER
            lsm_init_id = v7k_init['id']

        return Initiator(lsm_init_id, lsm_init_type, v7k_init['name'])

    def _map_lsm_init_id_to_v7k(self, lsm_init_id):
        gi = self._get_initiators()

        v7k_init_id = None
        for i in gi.itervalues():
            v7kinit = self._get_initiator(i['id'])
            if 'WWPN' in v7kinit and v7kinit['WWPN'] == lsm_init_id:
                v7k_init_id = v7kinit['id']

            if 'iscsi_name' in v7kinit and v7kinit['iscsi_name'] == \
                    lsm_init_id:
                v7k_init_id = v7kinit['id']

        if not v7k_init_id:
            raise LsmError(ErrorNumber.NOT_FOUND_INITIATOR,
                           "Initiator not found")

        return v7k_init_id

    def _get_volumes_accessible_by_initiator(self, init_id):
        # init_id passed is the lsm's init id. Convert it to v7k id.
        v7k_init_id = self._map_lsm_init_id_to_v7k(init_id)

        ssh_cmd = 'lshostvdiskmap -delim ! %s' % v7k_init_id
        return self._execute_command_and_parse_concise(ssh_cmd)

    def _get_initiators_granted_to_volume(self, v):
        ssh_cmd = 'lsvdiskhostmap -delim ! %s' % v
        return self._execute_command_and_parse_concise(ssh_cmd)

    def _initiator_create(self, init_id, init_type):
        if init_type == Initiator.TYPE_PORT_WWN:
            type_option = '-hbawwpn'
        elif init_type == Initiator.TYPE_ISCSI:
            type_option = '-iscsiname'
        else:
            raise LsmError(ErrorNumber.UNSUPPORTED_INITIATOR_TYPE,
                           "Initiator type not supported")

        ssh_cmd = 'mkhost %s %s' % (type_option, init_id)
        return self._execute_command(ssh_cmd)

    def _initiator_create_is_success(self, stdout):
        if 'successfully created' in stdout:
            return True
        return False

    def _initiator_grant(self, init_id, init_type, vol_id, force):
        # init_id passed is the lsm's init id. Convert it to v7k id.
        try:
            v7k_init_id = self._map_lsm_init_id_to_v7k(init_id)
        except LsmError as le:
            if le.code == ErrorNumber.NOT_FOUND_INITIATOR:
                # Auto add the initiator
                exit_code, stdout, stderr = self._initiator_create(init_id,
                                                                   init_type)
                if self._initiator_create_is_success(stdout):
                    # If success, get the v7k id for the new init
                    # This should not cause an exception this time!
                    v7k_init_id = self._map_lsm_init_id_to_v7k(init_id)
                else:
                    raise LsmError(ErrorNumber.PLUGIN_ERROR,
                                   "Error creating initiator")
            else:
                raise le

        ssh_cmd = 'mkvdiskhostmap %s -host %s %s' % \
                  ('-force' if force else '', v7k_init_id, vol_id)
        return self._execute_command(ssh_cmd)

    def _initiator_grant_is_success(self, stdout):
        if 'successfully created' in stdout:
            return True
        return False

    def _initiator_delete(self, v7k_init_id, force=True):
        ssh_cmd = 'rmhost %s %s' % ('-force' if force else '', v7k_init_id)
        self._execute_command(ssh_cmd)

    def _initiator_revoke(self, init_id, vol_id):
        # init_id passed is the lsm's init id. Convert it to v7k id.
        v7k_init_id = self._map_lsm_init_id_to_v7k(init_id)

        ssh_cmd = 'rmvdiskhostmap -host %s %s' % (v7k_init_id, vol_id)
        self._execute_command(ssh_cmd)

        # Auto remove initiator (if no mapping present)
        # v7k rmhost raises exception, if host is being removed with mappings
        # present... unless -force is specified. So exploit that fact here.
        try:
            self._initiator_delete(v7k_init_id, force=False)
        except LsmError:
            pass

        return

    def plugin_register(self, uri, password, timeout, flags=0):
        self.password = password
        self.tmo = timeout
        self.up = uri_parse(uri)

        self.ssh = SSHClient(self.up['host'], self.up['username'],
                             self.password, self.tmo)

        si = self._get_system_info()
        self.sys_info = System(si['id'], si['name'], System.STATUS_OK, '')

    def time_out_set(self, ms, flags=0):
        self.tmo = ms
        self.ssh.close()
        self.ssh = SSHClient(self.up['host'], self.up['username'],
                             self.password, self.tmo)

    def time_out_get(self, flags=0):
        return self.tmo

    def plugin_unregister(self, flags=0):
        self.ssh.close()
        return

    def job_status(self, job_id, flags=0):
        raise LsmError(ErrorNumber.NOT_IMPLEMENTED,
                       "API not implemented at this time")

    def job_free(self, job_id, flags=0):
        raise LsmError(ErrorNumber.NOT_IMPLEMENTED,
                       "API not implemented at this time")

    # NOTE: Add more capabilities as more cli's are supported
    def capabilities(self, system, flags=0):
        cap = Capabilities()
        cap.set(Capabilities.BLOCK_SUPPORT)
        cap.set(Capabilities.VOLUMES)
        cap.set(Capabilities.VOLUME_CREATE)
        cap.set(Capabilities.VOLUME_DELETE)
        cap.set(Capabilities.VOLUME_THIN)
        cap.set(Capabilities.INITIATORS)
        cap.set(Capabilities.VOLUME_ACCESSIBLE_BY_INITIATOR)
        cap.set(Capabilities.INITIATORS_GRANTED_TO_VOLUME)
        cap.set(Capabilities.VOLUME_INITIATOR_GRANT)
        cap.set(Capabilities.VOLUME_INITIATOR_REVOKE)
        return cap

    def plugin_info(self, flags=0):
        return "IBM V7000 lsm plugin", VERSION

    def pools(self, search_key=None, search_value=None, flags=0):
        gp = self._get_pools()
        return search_property(
            [self._pool(p) for p in gp.itervalues()],
            search_key, search_value)

    def systems(self, flags=0):
        return [self.sys_info]

    def volumes(self, search_key=None, search_value=None, flags=0):
        gv = self._get_volumes()
        return search_property(
            [self._volume(v) for v in gv.itervalues()],
            search_key, search_value)

    def initiators(self, flags=0):
        init_list = []
        gi = self._get_initiators()
        for i in gi.itervalues():
            init = self._get_initiator(i['id'])
            init_list.append(self._initiator(init))
        return init_list

    def volume_create(self, pool, volume_name, size_bytes, provisioning,
                      flags=0):
        self._create_volume(pool.id, volume_name, size_bytes, provisioning)
        new_vol = self._get_volume(volume_name)
        return None, self._volume(new_vol)

    def volume_delete(self, volume, flags=0):
        # TODO: How to pass -force param ? For now, assume -force
        self._delete_volume(volume.id, force=True)
        return None

    def initiator_grant(self, initiator_id, initiator_type, volume, access,
                        flags=0):
        # TODO: How to pass -force param ? For now, assume -force.
        #       -force allows multiple vdisk-to-host assignments,
        #       which are not normally allowed

        # NOTE: V7000 doesn't provide a way to pass access param,
        #       access is always rw, if ro, raise error.
        if access != Volume.ACCESS_READ_WRITE:
            raise LsmError(ErrorNumber.NO_SUPPORT,
                           "Only RW access to the volume is supported")

        exit_code, stdout, stderr = self._initiator_grant(initiator_id,
                                                          initiator_type,
                                                          volume.id,
                                                          force=True)
        return self._initiator_grant_is_success(stdout)

    def initiator_revoke(self, initiator, volume, flags=0):
        self._initiator_revoke(initiator.id, volume.id)
        return

    def volumes_accessible_by_initiator(self, initiator, flags=0):
        gvabi = self._get_volumes_accessible_by_initiator(initiator.id)

        vol_list = []
        for vol in gvabi.itervalues():
            gv = self._get_volume(vol['vdisk_id'])
            v = self._volume(gv)
            vol_list.append(v)

        return vol_list

    def initiators_granted_to_volume(self, volume, flags=0):
        gigtv = self._get_initiators_granted_to_volume(volume.id)

        init_list = []
        for init in gigtv.itervalues():
            gi = self._get_initiator(init['host_id'])
            i = self._initiator(gi)
            init_list.append(i)

        return init_list
