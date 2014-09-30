## Copyright (C) 2014 Red Hat, Inc.
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
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA
#
# Author: Gris Ge <fge@redhat.com>

from lsm import Disk, md5, LsmError, ErrorNumber
from dmtf import (
    DMTF, DMTF_DISK_TYPE_UNKNOWN, DMTF_DISK_TYPE_OTHER,
    DMTF_DISK_TYPE_HDD, DMTF_DISK_TYPE_SSD, DMTF_DISK_TYPE_HYBRID)
from utils import merge_list


_LSM_DISK_OP_STATUS_CONV = {
    DMTF.OP_STATUS_UNKNOWN: Disk.STATUS_UNKNOWN,
    DMTF.OP_STATUS_OK: Disk.STATUS_OK,
    DMTF.OP_STATUS_PREDICTIVE_FAILURE: Disk.STATUS_PREDICTIVE_FAILURE,
    DMTF.OP_STATUS_ERROR: Disk.STATUS_ERROR,
    DMTF.OP_STATUS_NON_RECOVERABLE_ERROR: Disk.STATUS_ERROR,
    DMTF.OP_STATUS_STARTING: Disk.STATUS_STARTING,
    DMTF.OP_STATUS_STOPPING: Disk.STATUS_STOPPING,
    DMTF.OP_STATUS_STOPPED: Disk.STATUS_STOPPED,
}


def _disk_status_of_cim_disk(cim_disk):
    """
    Convert CIM_DiskDrive['OperationalStatus'] to LSM
    Only return status, no status_info
    """
    if 'OperationalStatus' not in cim_disk:
        return Disk.STATUS_UNKNOWN

    return DMTF.dmtf_op_status_list_conv(
        _LSM_DISK_OP_STATUS_CONV, cim_disk['OperationalStatus'],
        Disk.STATUS_UNKNOWN, Disk.STATUS_OTHER)[0]


_DMTF_DISK_TYPE_2_LSM = {
    DMTF_DISK_TYPE_UNKNOWN: Disk.TYPE_UNKNOWN,
    DMTF_DISK_TYPE_OTHER: Disk.TYPE_OTHER,
    DMTF_DISK_TYPE_HDD: Disk.TYPE_HDD,
    DMTF_DISK_TYPE_SSD: Disk.TYPE_SSD,
    DMTF_DISK_TYPE_HYBRID: Disk.TYPE_HYBRID,
}


def _dmtf_disk_type_2_lsm_disk_type(dmtf_disk_type):
    if dmtf_disk_type in _DMTF_DISK_TYPE_2_LSM.keys():
        return _DMTF_DISK_TYPE_2_LSM[dmtf_disk_type]
    else:
        return Disk.TYPE_UNKNOWN


def _disk_id_of_cim_disk(cim_disk):
    if 'SystemName' not in cim_disk or \
       'DeviceID' not in cim_disk:
        raise LsmError(
            ErrorNumber.PLUGIN_BUG,
            "_disk_id_of_cim_disk(): Got cim_disk with no "
            "SystemName or DeviceID property: %s, %s" %
            (cim_disk.path, cim_disk.items()))

    return md5("%s%s" % (cim_disk['SystemName'], cim_disk['DeviceID']))


def cim_disk_pros():
    """
    Return all CIM_DiskDrive Properties needed to create a Disk object.
    """
    return ['OperationalStatus', 'Name', 'SystemName',
            'Caption', 'InterconnectType', 'DiskType', 'DeviceID']


def sys_id_of_cim_disk(cim_disk):
    if 'SystemName' not in cim_disk:
        raise LsmError(
            ErrorNumber.PLUGIN_BUG,
            "sys_id_of_cim_disk(): Got cim_disk with no "
            "SystemName property: %s, %s" %
            (cim_disk.path, cim_disk.items()))
    return cim_disk['SystemName']


def _pri_cim_ext_of_cim_disk(smis_common, cim_disk_path, property_list=None):
    """
    Usage:
        Find out the Primordial CIM_StorageExtent of CIM_DiskDrive
        In SNIA SMI-S 1.4 rev.6 Block book, section 11.1.1 'Base Model'
        quote:
        A disk drive is modeled as a single MediaAccessDevice (DiskDrive)
        That shall be linked to a single StorageExtent (representing the
        storage in the drive) by a MediaPresent association. The
        StorageExtent class represents the storage of the drive and
        contains its size.
    Parameter:
        cim_disk_path   # CIM_InstanceName of CIM_DiskDrive
        property_list   # a List of properties needed on returned
                        # CIM_StorageExtent
    Returns:
        cim_pri_ext     # The CIM_Instance of Primordial CIM_StorageExtent
    Exceptions:
        LsmError
            ErrorNumber.LSM_PLUGIN_BUG  # Failed to find out pri cim_ext
    """
    if property_list is None:
        property_list = ['Primordial']
    else:
        property_list = merge_list(property_list, ['Primordial'])

    cim_exts = smis_common.Associators(
        cim_disk_path,
        AssocClass='CIM_MediaPresent',
        ResultClass='CIM_StorageExtent',
        PropertyList=property_list)
    cim_exts = [p for p in cim_exts if p["Primordial"]]
    if len(cim_exts) == 1:
        # As SNIA commanded, only _ONE_ Primordial CIM_StorageExtent for
        # each CIM_DiskDrive
        return cim_exts[0]
    else:
        raise LsmError(ErrorNumber.PLUGIN_BUG,
                       "_pri_cim_ext_of_cim_disk(): "
                       "Got unexpected count of Primordial " +
                       "CIM_StorageExtent for CIM_DiskDrive: %s, %s " %
                       (cim_disk_path, cim_exts))


def cim_disk_to_lsm_disk(smis_common, cim_disk):
    """
    Convert CIM_DiskDrive to lsm.Disk.
    """
    # CIM_DiskDrive does not have disk size information.
    # We have to find out the Primodial CIM_StorageExtent for that.
    cim_ext = _pri_cim_ext_of_cim_disk(
        smis_common, cim_disk.path,
        property_list=['BlockSize', 'NumberOfBlocks'])

    status = _disk_status_of_cim_disk(cim_disk)
    name = ''
    block_size = Disk.BLOCK_SIZE_NOT_FOUND
    num_of_block = Disk.BLOCK_COUNT_NOT_FOUND
    disk_type = Disk.TYPE_UNKNOWN
    sys_id = sys_id_of_cim_disk(cim_disk)

    # These are mandatory
    # we do not check whether they follow the SNIA standard.
    if 'Name' in cim_disk:
        name = cim_disk["Name"]
    if 'BlockSize' in cim_ext:
        block_size = cim_ext['BlockSize']
    if 'NumberOfBlocks' in cim_ext:
        num_of_block = cim_ext['NumberOfBlocks']

    # SNIA SMI-S 1.4 or even 1.6 does not define anyway to find out disk
    # type.
    # Currently, EMC is following DMTF define to do so.
    if 'InterconnectType' in cim_disk:  # DMTF 2.31 CIM_DiskDrive
        disk_type = cim_disk['InterconnectType']
        if 'Caption' in cim_disk:
            # EMC VNX introduced NL_SAS disk.
            if cim_disk['Caption'] == 'NL_SAS':
                disk_type = Disk.TYPE_NL_SAS

    if disk_type == Disk.TYPE_UNKNOWN and 'DiskType' in cim_disk:
        disk_type = _dmtf_disk_type_2_lsm_disk_type(cim_disk['DiskType'])

    # LSI way for checking disk type
    if not disk_type and cim_disk.classname == 'LSIESG_DiskDrive':
        cim_pes = smis_common.Associators(
            cim_disk.path,
            AssocClass='CIM_SAPAvailableForElement',
            ResultClass='CIM_ProtocolEndpoint',
            PropertyList=['CreationClassName'])
        if cim_pes and cim_pes[0]:
            if 'CreationClassName' in cim_pes[0]:
                ccn = cim_pes[0]['CreationClassName']
                if ccn == 'LSIESG_TargetSATAProtocolEndpoint':
                    disk_type = Disk.TYPE_SATA
                if ccn == 'LSIESG_TargetSASProtocolEndpoint':
                    disk_type = Disk.TYPE_SAS

    disk_id = _disk_id_of_cim_disk(cim_disk)

    return Disk(disk_id, name, disk_type, block_size, num_of_block, status,
                sys_id)