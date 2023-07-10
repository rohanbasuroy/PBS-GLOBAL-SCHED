# coding: utf-8

# Copyright (C) 2003-2022 Altair Engineering, Inc. All rights reserved.
# Copyright notice does not imply publication.
#
# ALTAIR ENGINEERING INC. Proprietary and Confidential. Contains Trade Secret
# Information. Not for use or disclosure outside of Licensee's organization.
# The software and information contained herein may only be used internally and
# is provided on a non-exclusive, non-transferable basis. License may not
# sublicense, sell, lend, assign, rent, distribute, publicly display or
# publicly perform the software or other information provided herein,
# nor is Licensee permitted to decompile, reverse engineer, or
# disassemble the software. Usage of the software and other information
# provided by Altair(or its resellers) is only as explicitly stated in the
# applicable end user license agreement between Altair and Licensee.
# In the absence of such agreement, the Altair standard end user
# license agreement terms shall govern.

"""
PBS hook for identifying the topology of SX-Aurora architecture.
The hook does the ideal job placement by assigning vector
engines and handles most of the job events.

This hook services the following events:
- queuejob
- exechost_startup
- execjob_begin
- execjob_end
- execjob_epilogue
- execjob_launch
- execjob_postsuspend
- execjob_preresume
"""
import pbs
import sys
import os
import subprocess
import re
import glob
import time
import string
import traceback
import copy
import math
import fcntl
import tempfile

e = pbs.event()

# Define some globals that get set in main
PBS_EXEC = ''
PBS_HOME = ''
PBS_MOM_HOME = ''
PBS_MOM_JOBS = ''
JOB_SUBSTATE_SUSPEND = 43
JOB_SUBSTATE_SCHSUSP = 45
JOB_SUBSTATE_UNKNOWN = 'unknown'
SWAP_OUT = 0
SWAP_IN = 1

# ============================================================================
# Derived error classes
# ============================================================================


class UserError(Exception):
    """
    Base class for errors fixable by the user.
    """
    pass


class ConfigError(Exception):
    """
    Errors in configuration.
    """
    pass


class ProcessingError(Exception):
    """
    Errors in Hook Execution.
    """
    pass


# ============================================================================
# Utility functions
# ============================================================================

#
# FUNCTION caller_name
#
def caller_name():
    """
    Return the name of the calling function or method.
    """
    return str(sys._getframe(1).f_code.co_name)


#
# FUNCTION convert_size
#
def convert_size(value, units='b'):
    """
    Convert a string containing a size specification (e.g. "1m") to a
    string using different units (e.g. "1024k").
    This function only interprets a decimal number at the start of the string,
    stopping at any unrecognized character and ignoring the rest of the string.
    When down-converting (e.g. MB to KB), all calculations involve integers and
    the result returned is exact. When up-converting (e.g. KB to MB) floating
    point numbers are involved. The result is rounded up. For example:
    1023MB -> GB yields 1g
    1024MB -> GB yields 1g
    1025MB -> GB yields 1g  <-- This value was rounded up
    1600MB -> GB yields 2g  <-- This value was rounded up
    Pattern matching or conversion may result in exceptions.
    """
    mem_units = {'b': 0, 'k': 10, 'm': 20, 'g': 30, 't': 40, 'p': 50,
                 'e': 60, 'z': 70, 'y': 80}
    try:
        new = units[0].lower()
        if new not in mem_units:
            raise ValueError('Invalid unit value')
        result = re.match(r'([-+]?\d+)([bkmgtpezy]?)',
                          str(value).lower())
        if not result:
            raise ValueError('Unrecognized value')
        val, old = result.groups()
        if int(val) < 0:
            raise ValueError('Value may not be negative')
        if old not in mem_units:
            old = 'b'
        factor = mem_units[old] - mem_units[new]
        val = float(val)
        val *= 2 ** factor
        if (val - int(val)) > 0.5:
            val += 1.0
        val = int(val)
        # pbs.size() does not like units following zero
        if val <= 0:
            return '0'
        return str(val) + new
    except Exception:
        return None


#
# FUNCTION size_as_int
#
def size_as_int(value):
    """
    Convert a size string to an integer representation of size in bytes
    """
    return int(convert_size(value).rstrip(string.ascii_lowercase))


def expand_list(old):
    """
    Convert condensed list format (with ranges) to an expanded Python list.
    The input string is a comma separated list of digits and ranges.
    Examples include:
    0-3,8-11
    0,2,4,6
    2,5-7,10
    """
    new = []
    if isinstance(old, list):
        old = ",".join(list(map(str, old)))
    stripped = old.strip()
    if not stripped:
        return new
    for entry in stripped.split(','):
        if '-' in entry[1:]:
            start, end = entry.split('-', 1)
            for i in range(int(start), int(end) + 1):
                new.append(i)
        else:
            new.append(int(entry))
    return new


def exec_cmd(cmd, use_script=False):
    """
    Run given command and return output
    :param cmd: command to run
    :type cmd: list
    :param use_script: Indicate whether to use script file to run
                       command or directly run using Popen
    :type use_script: boolean
    """
    pbs.logmsg(pbs.EVENT_DEBUG4, "%s: Method called" % (caller_name()))
    if isinstance(cmd, list):
        pbs.logmsg(pbs.EVENT_DEBUG4, "Cmd is: %s" % " ".join(cmd))
    elif isinstance(cmd, str):
        pbs.logmsg(pbs.EVENT_DEBUG4, "Cmd is: %s" % cmd)

    path = ""
    if use_script:
        fd, path = tempfile.mkstemp(prefix="runcmd_",
                                    suffix=".sh")
        os.chmod(path, 0o700)
        cmd = "#!/bin/bash\n" + cmd
        os.write(fd, cmd.encode("utf-8"))
        os.close(fd)
        cmd = [path]
    try:
        process = subprocess.Popen(cmd, shell=False,
                                   stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE)
        output, err = process.communicate()
    except OSError:
        pbs.logmsg(pbs.EVENT_DEBUG, "Failed to execute: %s" %
                   ' '.join(cmd))
        return
    except ValueError:
        pbs.logmsg(pbs.EVENT_DEBUG, "Failed to execute: %s" %
                   ' '.join(cmd))
        pbs.logmsg(pbs.EVENT_DEBUG, "Invalid arguments passed.")
        return
    except Exception as exc:
        pbs.logmsg(pbs.EVENT_DEBUG,
                       str(traceback.format_exc().strip().splitlines()))
        return
    finally:
        if use_script and path:
            os.unlink(path)
    status = process.returncode
    if status != 0:
        pbs.logmsg(pbs.EVENT_DEBUG,
                   "Unable to run command: %s.\n err: %s" %
                   (' '.join(cmd), err))
        return None
    return output.decode('utf-8')


def initialize_resource(resc):
    """
    Return a properly cast zero value
    """
    if isinstance(resc, pbs.pbs_int):
        ret = pbs.pbs_int(0)
    elif isinstance(resc, pbs.pbs_float):
        ret = pbs.pbs_float(0)
    elif isinstance(resc, pbs.size):
        ret = pbs.size('0')
    elif isinstance(resc, int):
        ret = 0
    elif isinstance(resc, float):
        ret = 0.0
    elif isinstance(resc, list):
        ret = []
    elif isinstance(resc, dict):
        ret = {}
    elif isinstance(resc, tuple):
        ret = ()
    elif isinstance(resc, str):
        ret = ''
    else:
        raise ValueError('Unable to initialize unknown resource type')
    return ret

def get_linux_distro():
    """
    Get the Linux distribution
    """
    try:
        cmd = ["cat", os.path.join(os.sep, "etc", "os-release")]
        result = exec_cmd(cmd)
        if result == None:
            return
        else:
            lines = result.split("\n")
            for line in lines:
                if "VERSION" in line:
                    distro = line.split("=")[1]
                    pbs.logmsg(pbs.EVENT_DEBUG, "Linux distribution is %s"
                               % distro)
                    return distro[1]
    except IOError:
        pbs.logmsg(pbs.EVENT_DEBUG,
                   str(traceback.format_exc().strip().splitlines()))
    return


# ============================================================================
# Utility classes
# ============================================================================


#
# CLASS Lock
#
class Lock(object):
    """
    Implement a simple locking mechanism using a file lock
    """

    def __init__(self, path):
        self.path = path
        self.lockfd = None

    def getpath(self):
        """
        Return the path of the lock file.
        """
        return self.path

    def getlockfd(self):
        """
        Return the file descriptor of the lock file.
        """
        return self.lockfd

    def __enter__(self):
        self.lockfd = open(self.path, 'w')
        fcntl.flock(self.lockfd, fcntl.LOCK_EX)
        pbs.logmsg(pbs.EVENT_DEBUG4, '%s file lock acquired by %s' %
                   (self.path, str(sys._getframe(1).f_code.co_name)))

    def __exit__(self, exc, val, trace):
        if self.lockfd:
            fcntl.flock(self.lockfd, fcntl.LOCK_UN)
            self.lockfd.close()
        pbs.logmsg(pbs.EVENT_DEBUG4, '%s file lock released by %s' %
                   (self.path, str(sys._getframe(1).f_code.co_name)))


#
# CLASS SXAUtils
#
class SXAUtils:
    """
    SX Aurora utility methods
    """

    def __init__(self, hostname):
        self.hostname = hostname

    # NEC_PROCESS_DIST constraints
     
    def check_nec_process_dist(self, process_dist, chunks): 
        """
        Helper method for verifying chunks of NEC specific
        environment variable i.e. NEC_PROCESS_DIST
        """
        chunk_len = len(chunks)

        # Number of chunks in NEC_PROCESS_DIST and Resource_List.select
        # should be same
        if chunk_len != len(process_dist):
            e.reject("Number of chunks in select spec and "
                     "NEC_PROCESS_DIST does not match")

        # Process each chunk of NEC_PROCESS_DIST and select spec
        for chunk_num in range(chunk_len):
            mpiprocs = 0
            nves = 0
            select_chunk = chunks[chunk_num].split(":")
            for resc in select_chunk:
                if "mpiprocs" in resc:
                    mpiprocs = int(resc.split("=")[1])
                if "nves" in resc:
                    nves = int(resc.split("=")[1])

            # Check few constraints of NEC_PROCESS_DIST
            vh_procs, ve_procs = \
                self.check_vh_process_spec(process_dist[chunk_num])
            self.check_vh_process_and_mpiprocs(process_dist[chunk_num],
                                               vh_procs, ve_procs, mpiprocs)
            self.check_ve_process_spec(process_dist[chunk_num],
                                       ve_procs, nves)
            self.check_ve_process_and_mpiprocs(process_dist[chunk_num],
                                               vh_procs, ve_procs,
                                               mpiprocs, nves)

    def check_vh_process_spec(self, process_chunk):
        """
        Constraints for VH Process specification in NEC_PROCESS_DIST.
        Following are constraints and examples of invalid specifications.

        1. A chunk shall not have more than one VH Process specification
        i.e. NEC_PROCESS_DIST=s3:3:S2
        2. A chunk with VH process specification shall have a positive
        number for processes i.e. NEC_PROCESS_DIST=s0:3
        """
        is_vh_process = False
        vh_procs = 0
        ve_procs = []

        processes = process_chunk.split(":")
        for process in processes:
            # VH process
            if process[0] == "s" or process[0] == "S":
                # Constraint - 1
                if is_vh_process == 1:
                    e.reject("Please specify only one VH Process "
                             "in chunk %s of NEC_PROCESS_DIST"
                             % process_chunk)
                elif not is_vh_process:
                    is_vh_process = True

                try:
                    vh_procs = int(process[1:])
                except Exception as exc:
                    e.reject("Please specify integer VH process in "
                             "chunk %s of NEC_PROCESS_DIST"
                             % process_chunk)

                # Constraint - 2
                if vh_procs <= 0:
                    e.reject("VH processes should be greater than "
                             "0 in chunk %s of NEC_PROCESS_DIST"
                              % process_chunk)
            else:  # VE Process
                try:
                    num = int(process)
                    if num < 0:
                        raise
                    ve_procs.append(num)
                except Exception as exc:
                    e.reject("Please specify correct VE process in "
                             "chunk %s of NEC_PROCESS_DIST"
                             % process_chunk)
        return vh_procs, ve_procs
    
    def check_vh_process_and_mpiprocs(self, process_chunk, vh_procs,
                                      ve_procs, mpiprocs):
        """
        Constraints for VH Process amd mpiprocs.
        Following are constraints and examples of invalid specifications.

        1. In a chunk, the number of VH processes should be <= mpiprocs
        value i.e
        Ex - #PBS -l select=ncpus=4:mpiprocs=4:nves=1
             #PBS –v NEC_PROCESS_DIST=s5:3

        2. In a chunk, if number of VE processes are not specified,
        the number of VH processes should be equal to mpiprocs value
        Ex - #PBS –l select=ncpus=4:mpiprocs=4
             #PBS –v NEC_PROCESS_DIST=s3

        3. In a chunk, if number of VE processes is 0,
        then the VH processes should be equal to mpiprocs value
        Ex - #PBS –l select=ncpus=4:mpiprocs=4:nves=4
             #PBS –v NEC_PROCESS_DIST=s3:0

        """

        # Constraint - 1
        if vh_procs > mpiprocs:
            e.reject("Number of VH processes > mpiprocs in "
                     "chunk %s of NEC_PROCESS_DIST" % process_chunk)

        # Constraint - 2
        if len(ve_procs) == 0 and vh_procs != mpiprocs:
            e.reject("Number of VH processes != mpiprocs value "
                     "in chunk %s of NEC_PROCESS_DIST "
                     % process_chunk)

        # Constraint - 3
        if len(ve_procs) == 1 and ve_procs[0] == 0:
            if mpiprocs > 0 and vh_procs != mpiprocs:
                e.reject("For VE offloading, number of VH processes"
                         " and mpiprocs value should be equal in "
                         "chunk %s of NEC_PROCESS_DIST " % process_chunk)

    def check_ve_process_spec(self, process_chunk, ve_procs, nves):
        """
        Constraints for VE Process specification.
        Following are constraints and examples of invalid specifications.

        1. In Unequal VE process distribution, the number of colon
        seperated VE processes should be equal to Resource_List.nves
        Ex - #PBS –l select=mpiprocs=8:nves=2
             #PBS –v NEC_PROCESS_DIST=1:5:2

        2. For Unequal VE process distribution, all the colon seperated
        VE processes values should be greater than zero.
        Ex - #PBS –l select=mpiprocs=8:nves=2
             #PBS –v NEC_PROCESS_DIST=1:0
        """
        procs_len = len(ve_procs)

        # Constraint - 1
        if procs_len > 1 and nves != procs_len:
            e.reject("nves != to the number of VE processes "
                     "requested in chunk %s of NEC_PROCESS_DIST"
                     % process_chunk)

        # Contraint - 2
        if procs_len > 1 and 0 in ve_procs:
            e.reject("Number of VE Processes is not greater than"
                     " zero in chunk %s of NEC_PROCESS_DIST"
                     % process_chunk)

    def check_ve_process_and_mpiprocs(self, process_chunk, vh_procs,
                                      ve_procs, mpiprocs, nves):
        """
        Constraints for VE Process and mpiprocs
        Following are constraints and examples of invalid specifications.

        1. In a chunk, for equal VE process distribution,
        the number of VE processes should be equal to ceil(n/m),
        where n is the value of mpiprocs after subtracting VH processes
        and m is Resource_List.nves
        Ex - #PBS –l select=ncpus=2:mpiprocs=9:nves=2
             #PBS –v NEC_PROCESS_DIST=s2:3

        2. The sum of Unequal VE Process distribution and VH process
        should be equal to Resource_List.mpiprocs.
        Ex - #PBS –l select=mpiprocs=8:nves=2
             #PBS –v NEC_PROCESS_DIST=s4:1:4
        """
        # Constraint - 1
        if len(ve_procs) == 1:
            ve_mpiprocs = mpiprocs - vh_procs
            if ve_mpiprocs > 0 and nves == 0:
                e.reject("nves are not requested for chunk "
                         "%s of NEC_PROCESS_DIST" % process_chunk)
            if ve_procs[0] > 0 and nves > 0:
                if math.ceil(ve_mpiprocs / nves) != ve_procs[0]:
                    e.reject("Equal VE Process distribution is not "
                             "correct in chunk %s of "
                             "NEC_PROCESS_DIST" % process_chunk)

        # Constraint - 2
        if len(ve_procs) > 1:
            total_procs = sum(ve_procs) + vh_procs
            if total_procs != mpiprocs:
                e.reject("Sum of VH-VE Processes in chunk %s of "
                         "NEC_PROCESS_DIST is not equal to mpiprocs"
                         % process_chunk)

    def get_state_ves(self, ves_available):
        """
        Method to get state of VEs on vector host
        """
        pbs.logmsg(pbs.EVENT_DEBUG4, '%s: Method called' % caller_name())
        vecmd = os.path.join(os.sep, "opt", "nec", "ve", "bin", "vecmd")
        state = ["state", "GET"]
        # list of VEs in online state
        online_ves = []
        for ve in ves_available:
            ve_num = ve[-1]
            cmd = [vecmd, "-N", ve_num]
            cmd.extend(state)
            output = exec_cmd(cmd)
            if output == None:
                msg = "State GET for %s failed" % ve
                pbs.logmsg(pbs.EVENT_DEBUG, '%s' % msg)
                continue
            elif "ONLINE" in output:
                online_ves.append(ve)
            else:
                msg = "%s is not in Online state" % ve
                pbs.logmsg(pbs.EVENT_DEBUG, '%s' % msg)
        dup_ves_avail = copy.deepcopy(ves_available)
        for ve in dup_ves_avail.keys():
            if ve not in online_ves:
                del ves_available[ve]
        return ves_available

    def _discover_ves_and_ibs(self):
        """
        Identify ves, to which pcies and numa nodes they are attached
        using "/opt/nec/ve/bin/vecmd topo tree"
        """
        pbs.logmsg(pbs.EVENT_DEBUG4, '%s: Method called' % caller_name())
        ves = {}
        ibs = {}
        pci_bus_id = ""
        cmd = ["/opt/nec/ve/bin/vecmd", "topo", "tree"]
        output = exec_cmd(cmd)
        if output is None:
            return ves, ibs
        # Parse the VE devices and IB devices
        # and find out the associated numa nodes
        # and PCIe bus id
        lines = output.splitlines()
        for line in lines:
            if ('SOCKET' in line) and ('+-' in line):
                ve = ""
                ib_name = ""
                # Split line by '-+-'. 4 elements after
                # splitting indicates a new PCI tree which
                # contains PCI Bus Id at 2nd position
                elements = line.strip().lstrip('-+-').split('-+-')
                last_element = elements[-1]
                if (len(elements) == 4) and (':' in elements[1]) and (
                        '.' in elements[1]):
                    pci_bus_id = elements[1]
                if 'RE' in last_element:
                    continue
                last = last_element.split()
                for each in last:
                    if 'VE' in each:
                        ve = each.strip("[").strip("]")
                        ves[ve] = {"pci": pci_bus_id}
                    if 'IB' in each:
                        ib_name = last[-1]
                        ibs[ib_name] = {"pci": pci_bus_id}
                    if "SOCKET" in each:
                        socket = each.strip("[").split("]")[0]
                        if ve:
                            ves[ve]["numa_node"] = int(
                                socket.lstrip("SOCKET"))
                        if ib_name:
                            ibs[ib_name]["numa_node"] = int(
                                socket.lstrip("SOCKET"))
        if e.type != pbs.EXECHOST_STARTUP:
            ves = self.get_state_ves(ves)
        pbs.logmsg(pbs.EVENT_DEBUG, "VE devices info: %s" % ves)
        pbs.logmsg(pbs.EVENT_DEBUG, "IB devices info: %s" % ibs)
        return ves, ibs

    def discover_infini(self):
        """
        Method to extract HCA information on the host using "ibv_devinfo"
        """
        pbs.logmsg(pbs.EVENT_DEBUG, '%s: Method called' % caller_name())
        infini = ""
        cmd = ["ibv_devinfo"]
        output = exec_cmd(cmd)
        if output == None:
            pbs.logmsg(pbs.EVENT_DEBUG, "Failed to execute cmd: %s"
                       % " ".join(cmd))
            return None
        infini_list = output.split('\n')
        hca_list = []
        port_list = []
        link_layer_list = []
        for el in infini_list:
            if 'hca_id:\t' in el:
                hca_list.append(el.split('\t')[-1])
            if '\tport:\t' in el:
                port_list.append(":" + el.split(':')[-1].strip('\t'))
            if '\tlink_layer:\t\t' in el:
                link_layer_list.append(el.split(':')[-1].strip('\t'))
        hca_dist = {}
        i = 0
        pbs.logmsg(pbs.EVENT_DEBUG, ' hca_list = %s and port_list = %s'
                   % (hca_list, port_list))
        pbs.logmsg(pbs.EVENT_DEBUG, 'link_layer_list = %s' % link_layer_list)
        for hca in hca_list:
            if link_layer_list[i] == "Ethernet":
                i += 1
                continue
            hca_dist[hca] = ""
            hca_dist[hca] = port_list[i]
            i += 1
        pbs.logmsg(pbs.EVENT_DEBUG, 'hca_dist = %s' % hca_dist)
        return hca_dist

    def swap_ve_processes(self, swap_flag, pids=None):
        """
        For all the child pids fetched on this node for the job,
        try to swap out/in each of them, VE processes would be
        swapped out using "/opt/nec/ve/bin/veswap -o/-i <pid>".
        """
        procs = []

        try:
            cmd = ""
            if swap_flag == SWAP_OUT:
                cmd = ["/opt/nec/ve/bin/veswap", "-o"]
            elif swap_flag == SWAP_IN:
                cmd = ["/opt/nec/ve/bin/veswap", "-i"]
            if not cmd:
                raise Exception

            for pid in pids:
                swap_cmd = cmd + [pid]
                p = subprocess.Popen(swap_cmd, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE)
                result, error = p.communicate()
                if p.returncode == 0:
                    procs.append(pid)
        except Exception as exc:
            return []

        return procs

    def ve_accounting_metrics(self, ves, parent_pid):
        """
        Get the accounting metrics i.e. cpu consumption 
        and memory consumption for each VE Process on this
        host using
        "/opt/nec/ve/sbin/dump-acct --ve-info
        /var/opt/nec/ve/account/pacct_<ve_num>"
        """
        total_pcput = 0
        total_pmem = 0
        distro = get_linux_distro()
        if distro == None:
            pbs.logmsg(pbs.EVENT_DEBUG, "Unable to identify OS distribution"
                                        " for accounting")
            return None, None

        try:
            for ve in ves:
                cmd = "/opt/nec/ve/sbin/dump-acct --ve-info " \
                      "/var/opt/nec/ve/account/pacct_%s | grep %s" % \
                      (ve, str(parent_pid))
                result = exec_cmd(cmd, use_script=True)
                if result == None:
                    return None, None
                result = result.strip().split("\n")
                for record in result:
                    process_stats = record.split("|")
                    # process_stats is a list with index 2 for cpu
                    # time and index 14 for memory consumption.
                    # In RHEL-8, index 17 is for memory consumption,
                    # Hence, there is a check for OS distribution
                    # cpu time is in ticks, Convert it to seconds.
                    total_pcput += (float(process_stats[2]) / 100)
                    if distro == "8":
                        total_pmem += int(process_stats[17])
                    else:
                        total_pmem += int(process_stats[14])
        except Exception as exc:
            pbs.logmsg(pbs.EVENT_DEBUG,
                       str(traceback.format_exc().strip().splitlines()))
            return None, None
        return total_pcput, total_pmem

    def check_for_nqsv(self):
        """
        Check if scatefs(NEC's file system) is used
        """
        try:
            cmd = "mount | grep scatefs"
            result = exec_cmd(cmd, use_script=True)
            if result == None:
                pbs.logmsg(pbs.EVENT_DEBUG, "Scatefs is not enabled")
            else:
                if "type scatefs" in result:
                    pbs.logmsg(pbs.EVENT_DEBUG, "Scatefs is enabled")
                    return True
                else:
                    pbs.logmsg(pbs.EVENT_DEBUG, "Scatefs is not enabled")
        except Exception as exc:
            pbs.logmsg(pbs.EVENT_DEBUG,
                       str(traceback.format_exc().strip().splitlines()))
        return False
      
    
#
# CLASS HookUtils
#
class HookUtils(object):
    """
    Hook utility methods
    """

    def __init__(self, hook_events=None):
        if hook_events != None:
            self.hook_events = hook_events
        else:
            # Defined in the order they appear in module_pbs_v1.c
            self.hook_events = {}
            self.hook_events[pbs.QUEUEJOB] = {
                'name': 'queuejob',
                'handler': self._queuejob_handler
            }
            self.hook_events[pbs.MODIFYJOB] = {
                'name': 'modifyjob',
                'handler': None
            }
            self.hook_events[pbs.RESVSUB] = {
                'name': 'resvsub',
                'handler': None
            }
            self.hook_events[pbs.MOVEJOB] = {
                'name': 'movejob',
                'handler': None
            }
            self.hook_events[pbs.RUNJOB] = {
                'name': 'runjob',
                'handler': None
            }
            self.hook_events[pbs.PROVISION] = {
                'name': 'provision',
                'handler': None
            }
            self.hook_events[pbs.EXECJOB_BEGIN] = {
                'name': 'execjob_begin',
                'handler': self._execjob_begin_handler
            }
            self.hook_events[pbs.EXECJOB_PROLOGUE] = {
                'name': 'execjob_prologue',
                'handler': None
            }
            self.hook_events[pbs.EXECJOB_EPILOGUE] = {
                'name': 'execjob_epilogue',
                'handler': self._execjob_epilogue_handler
            }
            self.hook_events[pbs.EXECJOB_PRETERM] = {
                'name': 'execjob_preterm',
                'handler': None
            }
            self.hook_events[pbs.EXECJOB_END] = {
                'name': 'execjob_end',
                'handler': self._execjob_end_handler
            }
            self.hook_events[pbs.EXECJOB_LAUNCH] = {
                'name': 'execjob_launch',
                'handler': self._execjob_launch_handler
            }
            self.hook_events[pbs.EXECHOST_PERIODIC] = {
                'name': 'exechost_periodic',
                'handler': self._exechost_periodic_handler
            }
            self.hook_events[pbs.EXECHOST_STARTUP] = {
                'name': 'exechost_startup',
                'handler': self._exechost_startup_handler
            }
            self.hook_events[pbs.EXECJOB_ATTACH] = {
                'name': 'execjob_attach',
                'handler': None
            }
            if hasattr(pbs, "EXECJOB_RESIZE"):
                self.hook_events[pbs.EXECJOB_RESIZE] = {
                    'name': 'execjob_resize',
                    'handler': None
                }
            if hasattr(pbs, "EXECJOB_ABORT"):
                self.hook_events[pbs.EXECJOB_ABORT] = {
                    'name': 'execjob_abort',
                    'handler': None
                }
            if hasattr(pbs, "EXECJOB_POSTSUSPEND"):
                self.hook_events[pbs.EXECJOB_POSTSUSPEND] = {
                    'name': 'execjob_postsuspend',
                    'handler': self._execjob_postsuspend_handler
                }
            if hasattr(pbs, "EXECJOB_PRERESUME"):
                self.hook_events[pbs.EXECJOB_PRERESUME] = {
                    'name': 'execjob_preresume',
                    'handler': self._execjob_preresume_handler
                }
            self.hook_events[pbs.MOM_EVENTS] = {
                'name': 'mom_events',
                'handler': None
            }

    def event_name(self, hooktype):
        """
        Return the event name for the supplied hook type.
        """
        pbs.logmsg(pbs.EVENT_DEBUG4, '%s: Method called' % caller_name())
        if hooktype in self.hook_events:
            return self.hook_events[hooktype]['name']
        pbs.logmsg(pbs.EVENT_DEBUG4,
                   '%s: Type: %s not found' % (caller_name(), type))
        return None

    def hashandler(self, hooktype):
        """
        Return the handler for the supplied hook type.
        """
        pbs.logmsg(pbs.EVENT_DEBUG4, '%s: Method called' % caller_name())
        if hooktype in self.hook_events:
            return self.hook_events[hooktype]['handler'] != None
        return None

    def invoke_handler(self, sxautils, jobutil, *args):
        """
        Call the appropriate handler for the supplied event.
        """
        pbs.logmsg(pbs.EVENT_DEBUG4, '%s: Method called' % caller_name())
        pbs.logmsg(pbs.EVENT_DEBUG4, '%s: UID: real=%d, effective=%d' %
                   (caller_name(), os.getuid(), os.geteuid()))
        pbs.logmsg(pbs.EVENT_DEBUG4, '%s: GID: real=%d, effective=%d' %
                   (caller_name(), os.getgid(), os.getegid()))
        if self.hashandler(e.type):
            return self.hook_events[e.type]['handler'](sxautils, jobutil, *args)
        return False

    def _queuejob_handler(self, sxautils, jobutil):
        """
        Handler for queuejob event - Process the NEC_PROCESS_DIST
        environment variable.
        """
        pbs.logmsg(pbs.EVENT_DEBUG4, '%s: Method called' % caller_name())
        job = e.job

        # Check if user has asked for NEC_PROCESS_DIST
        vlist = re.split(r'(?<!\\),', str(job.Variable_List))
        process_dist = ""
        for var in vlist:
            if var.startswith('NEC_PROCESS_DIST'):
                process_dist = var.split("=")[1].split("+")
                pbs.logmsg(pbs.EVENT_DEBUG, 'NEC_PROCESS_DIST = %s'
                           % process_dist)
                break

        # Return if no NEC_PROCESS_DIST has been requested for
        if len(process_dist) == 0:
            return True

        chunks = str(job.Resource_List.select).split('+')
        pbs.logmsg(pbs.EVENT_DEBUG, 'select=%s' % chunks)

        sxautils.check_nec_process_dist(process_dist, chunks)

        return True

    def _exechost_startup_handler(self, sxautils, jobutil):
        """
        Handler for exechost_startup events.
        """
        pbs.logmsg(pbs.EVENT_DEBUG4, '%s: Method called' % caller_name())
        try:
            ves, ibs = sxautils._discover_ves_and_ibs()
        except Exception as exc:
            pbs.logmsg(pbs.EVENT_DEBUG,
                       str(traceback.format_exc().strip().splitlines()))
            e.reject("Unable to identify VE and IB devices")
        if not ves or not ibs:
            e.reject('%s: Failed to identify VE and IB devices' %
                     caller_name())
        node = NodeUtils(ves=ves, ibs=ibs)
        pbs.logmsg(pbs.EVENT_DEBUG4, '%s: NodeUtils class instantiated' %
                   caller_name())
        node.create_vnodes()
        return True

    def _exechost_periodic_handler(self, sxautils, jobutil):
        """
        Handler for exechost_periodic event.
        """
        pbs.logmsg(pbs.EVENT_DEBUG4, '%s: Method called' % caller_name())
        try:
            ves, ibs = sxautils._discover_ves_and_ibs()
        except Exception as exc:
            pbs.logmsg(pbs.EVENT_DEBUG,
                       str(traceback.format_exc().strip().splitlines()))
            e.reject("Unable to identify VE and IB devices")
        node = NodeUtils(ves=ves, ibs=ibs)
        pbs.logmsg(pbs.EVENT_DEBUG4, '%s: NodeUtils class instantiated' %
                   caller_name())
        node.update_vnodes()
        return True        

    def _execjob_begin_handler(self, sxautils, jobutil):
        """
        Handler for execjob_begin events.
        """
        pbs.logmsg(pbs.EVENT_DEBUG, '%s: Method called' % caller_name())
        try:
            ves, ibs = sxautils._discover_ves_and_ibs()
        except Exception as exc:
            pbs.logmsg(pbs.EVENT_DEBUG,
                       str(traceback.format_exc().strip().splitlines()))
            e.reject("Unable to identify VE and IB devices")
        if not ves or not ibs:
            e.reject('%s: Failed to identify VE and IB devices' %
                     caller_name())
        node = NodeUtils(ves=ves, ibs=ibs)
        assigned_res = jobutil.assigned_resources
        try:
            if assigned_res['nves']:
                pbs.logmsg(pbs.EVENT_DEBUG,
                           '%s: Host assigned job resources: %s' %
                           (caller_name(), assigned_res))
        except KeyError:
            pbs.logmsg(pbs.EVENT_DEBUG,
                       'No nves requested in job')
            return True
        
        already_assigned_ves = jobutil._get_assigned_ves(e.job.id)
        job_ves = jobutil._assign_ves(node.nodes, already_assigned_ves)
        path = jobutil.node_file(job_ves, node.nodes)
        try:
            hca_dist = sxautils.discover_infini()
            if hca_dist == None:
                e.reject("Unable to identify Infiniband Information")
        except Exception as exc:
            pbs.logmsg(pbs.EVENT_DEBUG,
                       str(traceback.format_exc().strip().splitlines()))
            e.reject("Unable to identify Infiniband Information")
        nqsv_flag = sxautils.check_for_nqsv()
        jobutil._write_job_env_file(job_ves, node.nodes, path, nqsv_flag, hca_dist)
        return True

    def _execjob_launch_handler(self, sxautils, jobutil):
        """
        Handler for execjob_launch event.
        """
        pbs.logmsg(pbs.EVENT_DEBUG4, '%s: Method called' % caller_name())

        # If the node is sister, create a pid file that stores the 
        # parent pid of the job on this node. This file shall be used by 
        # suspend/resume hook events to fetch the child pids of the job
        # on this node for swap-out and swap-in operations. 

        if not e.job.in_ms_mom():
            try:
                filename = jobutil.host_job_ppid % e.job.id
                with open(filename, 'w') as desc:
                    desc.write(str(os.getppid()))
            except Exception:
                e.reject("Unable to create the pid file on this node")
        # export NEC env vars
        jobutil.setup_job_devices_env()
        return True

    def suspend_resume_handler(self, sxautils, jobutil, swap_flag, swap_str):
        """
        Common method for handling both suspend/resume functionality
        """
        job = e.job

        # Find out all the child pids of the job. Ideally, hook should be
        # fetching VE pids, but there is no way to differentiate between
        # VH and VE processes of a job so fetching all the child pids.

        pids = jobutil.get_child_pids()
        if pids == None:
            pbs.logmsg(pbs.EVENT_DEBUG,
                       '%s: Unable to fetch pids of job' %
                       caller_name())
            return True

        pids = [pid.strip() for pid in pids.split("\n")]
        procs = sxautils.swap_ve_processes(swap_flag, pids)
        if len(procs) > 0:
            pbs.logmsg(pbs.EVENT_DEBUG,
                       "%s: The VE processes of job %s %s on this node - %s" %
                       (caller_name(), job.id, swap_str, procs))
        else:
            pbs.logmsg(pbs.EVENT_DEBUG,
                       "%s: No VE processes %s for %s" %
                       (caller_name(), swap_str, job.id))
        return True

    def _execjob_postsuspend_handler(self, sxautils, jobutil):
        """
        Handler for execjob_postsuspend event - VE processes of a job
        need to be swapped out to vector host.
        """
        pbs.logmsg(pbs.EVENT_DEBUG4, '%s: Method called' % caller_name())
        self.suspend_resume_handler(sxautils, jobutil, SWAP_OUT,
                                    "swapped-out")
        return True   

    def _execjob_preresume_handler(self, sxautils, jobutil):
        """
        Handler for execjob_preresume event - VE processes of a job
        need to be swapped in.
        """
        pbs.logmsg(pbs.EVENT_DEBUG4, '%s: Method called' % caller_name())
        self.suspend_resume_handler(sxautils, jobutil, SWAP_IN,
                                    "swapped-in")
        return True

    def _execjob_epilogue_handler(self, sxautils, jobutil):
        """
        Handler for execjob_epilogue event - Calculate the VE
        processes cpu time and memory consumption and update
        the job's resources_used attribute.
        """
        pbs.logmsg(pbs.EVENT_DEBUG4, '%s: Method called' % caller_name())
        job = e.job
        ves = []

        # Get the VE numbers assigned to the job on this node
        try:
            fn = jobutil.host_job_env_filename % job.id
            with open(fn, 'r') as desc:
                ve_env_list = desc.readlines()
            
            for ve_env in ve_env_list:
                if "_VENODELIST" in ve_env:
                    ves = ve_env.strip("\n").split("=")[1]
                    ves = ves.strip('"').split(" ")     
                    pbs.logmsg(pbs.EVENT_DEBUG, "VE's : %s" % ves)
        except Exception as exc:
            pbs.logmsg(pbs.EVENT_DEBUG, '%s: Failed to fetch VEs for job %s'
                       % (caller_name(), job.id))
            return True
     
        if len(ves) == 0:
            pbs.logmsg(pbs.EVENT_DEBUG,
                       "%s: No VE's are found for job %s"
                       % (caller_name(), job.id))
            return True
          
        # The hook will use the parent pid for grepping the VE processes
        # usage metrics. Thus, get the parent pid on each node for the job.
        parent_pid = jobutil.get_parent_pid()
        if not parent_pid:
            pbs.logmsg(pbs.EVENT_DEBUG,
                       "%s: VE Process accounting is not added in "
                       "this node for %s" % (caller_name(), job.id))
            return True

        pcput, pmem = sxautils.ve_accounting_metrics(ves, parent_pid)  
        if pcput == None and pmem == None:
            pbs.logmsg(pbs.EVENT_DEBUG,
                       '%s: Unable to fetch VE accounting metrics'
                       % caller_name())
            return True

        job.resources_used["ve_cput"] = pbs.duration(pcput)

        # The unit of memory usage is in kilobytes
        job.resources_used["ve_mem"] = pbs.size(str(pmem) + "kb")
        pbs.logmsg(pbs.EVENT_DEBUG4, '%s: Updated resc_used = %s' %
                   (caller_name(), str(job.resources_used)))
        return True

    def _execjob_end_handler(self, sxautils, jobutil):
        """
        Handler for execjob_end event - Cleaning up the job's parent pid
        file on sister moms, environment files and second nodefile on all
        hosts.
        """
        pbs.logmsg(pbs.EVENT_DEBUG4, '%s: Method called' % caller_name())
        job = e.job 
        if not job.in_ms_mom():
            fn = jobutil.host_job_ppid % job.id
            if os.path.isfile(fn):
                os.remove(fn)
                pbs.logmsg(pbs.EVENT_DEBUG4, "%s: Removing the parent pid "
                                             "file %s" % (caller_name(), fn))
        filelist = []
        if pbs.pbs_conf['PBS_MOM_HOME']:
            home_dir = pbs.pbs_conf['PBS_MOM_HOME']
        else:
            home_dir = pbs.pbs_conf['PBS_HOME']
        path = os.path.join(home_dir, 'aux', job.id + '.ve_nodefile')
        filelist.append(path)        
        filelist.append(jobutil.host_job_env_filename % job.id)
        for filename in filelist:
            try:
                os.remove(filename)
            except OSError:
                pbs.logmsg(pbs.EVENT_DEBUG4,
                           'File: %s not found' % filename)
            except Exception:
                pbs.logmsg(pbs.EVENT_DEBUG4,
                           'Error removing file: %s' % filename)
        return True


#
# CLASS JobUtils
#
class JobUtils:
    """
    Job utility methods
    """

    def __init__(self, job, hostname=None, assigned_resources=None):
        self.job = job
        self.host_job_env_dir = os.path.join(PBS_MOM_HOME, 'aux')
        self.hook_storage_dir = os.path.join(PBS_MOM_HOME, 'mom_priv',
                                             'hooks', 'hook_data')

        if not os.path.isdir(self.hook_storage_dir):
            try:
                os.makedirs(self.hook_storage_dir, 0o700)
            except OSError:
                pbs.logmsg(pbs.EVENT_DEBUG, 'Failed to create %s' %
                           self.hook_storage_dir)
        self.host_job_env_filename = os.path.join(self.host_job_env_dir,
                                                  '%s.env')
        self.host_job_ppid = os.path.join(self.hook_storage_dir, '%s.ppid')
        
        if hostname != None:
            self.hostname = hostname
        else:
            self.hostname = pbs.get_local_nodename()

        if assigned_resources != None:
            self.assigned_resources = assigned_resources
        else:
            self.assigned_resources = self._get_assigned_job_resources()

    def job_is_suspended(self, jobid):
        """
        Returns True if job is in a suspended or unknown substate
        """
        jobinfo = self.printjob_info(jobid)
        if 'substate' in jobinfo:
            return jobinfo['substate'] in \
                   [JOB_SUBSTATE_SUSPEND, JOB_SUBSTATE_SCHSUSP,
                    JOB_SUBSTATE_UNKNOWN]
        return False

    def printjob_info(self, jobid):
        """
        Use printjob to acquire the job information
        """
        info = {}
        jobfile = os.path.join(PBS_MOM_JOBS, '%s.JB' % jobid)
        if not os.path.isfile(jobfile):
            pbs.logmsg(pbs.EVENT_DEBUG4, 'File not found: %s' % jobfile)
            return info
        cmd = [os.path.join(PBS_EXEC, 'bin', 'printjob')]
        cmd.append('-a')
        cmd.append(jobfile)
        try:
            pbs.logmsg(pbs.EVENT_DEBUG4, 'Running: %s' % cmd)
            process = subprocess.Popen(cmd, shell=False,
                                       stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE,
                                       universal_newlines=True)
            out, err = process.communicate()
            if process.returncode != 0:
                pbs.logmsg(pbs.EVENT_DEBUG4,
                           'command return code non-zero: %s'
                           % str(process.returncode))
                pbs.logmsg(pbs.EVENT_DEBUG4,
                           'command stderr: %s'
                           % err)
        except Exception as exc:
            pbs.logmsg(pbs.EVENT_DEBUG2, 'Error running command: %s' % cmd)
            pbs.logmsg(pbs.EVENT_DEBUG2, 'Exception: %s' % exc)
            return {}
        out_split = out.splitlines()
        pattern = re.compile(r'^(\w.*):\s*(\S+)')
        for line in out_split:
            result = re.match(pattern, line)
            if not result:
                continue
            key, val = result.groups()
            if not key or not val:
                continue
            if val.startswith('0x'):
                info[key] = int(val, 16)
            elif val.isdigit():
                info[key] = int(val)
            else:
                info[key] = val
        return info

    def gather_jobs_on_node(self):
        """
        Gather the jobs assigned to this node
        """
        pbs.logmsg(pbs.EVENT_DEBUG, '%s: Method called' % caller_name())
        joblist = []
        try:
            for jobfile in glob.glob(os.path.join(PBS_MOM_JOBS, '*.JB')):
                (jobid, dot_jb) = os.path.splitext(os.path.basename(jobfile))
                joblist.append(jobid)
        except Exception:
            pbs.logmsg(pbs.EVENT_DEBUG, 'Could not get job list for %s' %
                       self.hostname)
            e.reject("Unable to find jobs on this node.")
        pbs.logmsg(pbs.EVENT_DEBUG, 'Local job dictionary: %s' % str(joblist))
        return joblist

    def _get_assigned_ves(self, curr_job_id):
        """
        Get the VE's assigned on this node for other jobs
        """
        pbs.logmsg(pbs.EVENT_DEBUG, '%s: Method called' % caller_name())
        assigned_ves = []
        venodelist_value = ""
        lines = []
        for job in self.gather_jobs_on_node():
            if job == curr_job_id:
                continue
            if self.job_is_suspended(job):
                pbs.logmsg(pbs.EVENT_DEBUG, 'Job %s is suspended' % job)
                continue
            filename = self.host_job_env_filename % job
            try:
                with open(filename, 'r') as f1:
                    lines = f1.readlines()
                for line in lines:
                    if '_VENODELIST' in line:
                        pbs.logmsg(pbs.EVENT_DEBUG, "Job %s : %s" % (job, line))
                        venodelist_value += line.split('=')[1].strip("\n") + " "
            except IOError:
                pbs.logmsg(pbs.EVENT_SYSTEM, '%s: %s file not found' %
                           (caller_name(), filename))
        assigned_ves = venodelist_value.strip(" ").split(" ")
        pbs.logmsg(pbs.EVENT_DEBUG, 'already assigned ves: %s' % assigned_ves)
        return assigned_ves

    def node_file(self, ves, nodes):
        """
        Parse NEC_PROCESS_DIST and create the 2nd node file for NEC MPI
        If NEC_PROCESS_DIST is passed to the job then a
        block of ceil(m/n) processes should be assigned to each VE node
        where m is mpiprocs and n is number of ves requested in each chunk
        """
        pbs.logmsg(pbs.EVENT_DEBUG, '%s: Method called' % caller_name())

        # for VE offloading check 
        global ve_offloading
        ve_offloading = 0
        global offloading_ves
        offloading_ves = []

        # Look for NEC_PROCESS_DIST
        varlist = str(self.job.Variable_List)
        vlist = re.split(r'(?<!\\),', varlist)
        nec_procs_env = ""
        for i in vlist:
            if i.startswith('NEC_PROCESS_DIST'):
                name = i.split("=")
                nec_procs_env = name[1]
                break

        if nec_procs_env:
            if '0' in nec_procs_env:
                ve_offloading = 1

        # create a dict with select statement
        # and filter out mpiprocs and nves values
        nodefile_dist = {}
        select_chunks = str(self.job.schedselect).split("+")
        i = 1
        for chunk in list(select_chunks):
            multiple = 1
            res = chunk.split(":")
            if res[0].isdigit():
                multiple = int(res[0])
            # Here, the variable multiple=3 for a chunk 3:nves=1 i.e.
            # the number of duplicate chunks requested.
            # this helps with mapping select statement with
            # exec_vnode output and NEC_PROCESS_DIST
            while multiple > 0:
                key = "chunk" + str(i)
                i += 1
                nodefile_dist[key] = {}
                nodefile_dist[key]['select'] = chunk
                for r in res:
                    if "mpiprocs" in r:
                        val = int((r.split("=")[1]))
                        nodefile_dist[key]['mpiprocs'] = val
                    if "nves" in r:
                        val = int((r.split("=")[1]))
                        nodefile_dist[key]['nves'] = val
                multiple -= 1

        pbs.logmsg(pbs.EVENT_DEBUG, 'nodefile_dist = %s' % nodefile_dist)
        
        # parse exec_vnode of the job
        execv_chunks = re.findall(r'\(.*?\)', str(self.job.exec_vnode))
        pbs.logmsg(pbs.EVENT_DEBUG, 'execv_chunks=%s' % execv_chunks)

        if len(nodefile_dist) != len(execv_chunks):
            # probably a -lplace=exclhost job
            pbs.logmsg(pbs.EVENT_DEBUG, 'check for -lplace=exclhost')
            
        i = 0
        chunks = list(nodefile_dist)
        for vnode in execv_chunks:
            if "ncpus" in vnode:
                chunk = chunks[i]
                nodefile_dist[chunk]['execv'] = vnode
                i += 1
            else:
                continue

        pbs.logmsg(pbs.EVENT_DEBUG, 'nodefile_dist with execv: %s' % nodefile_dist)

        if nec_procs_env:
            nec_dist = nec_procs_env.split('+')
        else:
            # build nec_dist using nves and mpiprocs in nodefile_dist
            nec_dist = []
            for chunk in select_chunks:
                node_dist_ch = {}
                for ch in nodefile_dist:
                    if chunk in nodefile_dist[ch]['select']:
                        node_dist_ch = nodefile_dist[ch]
                if "nves" not in node_dist_ch.keys():
                    continue
                # Not likely but just to be safe because if nves
                # is requested then mpiprocs is also included in job statement
                if "mpiprocs" not in node_dist_ch.keys():
                    continue
                multiple = 1
                res = chunk.split(":")
                if res[0].isdigit():
                    multiple = int(res[0])
                nves_in_select = node_dist_ch['nves']
                mpiprocs_in_select = node_dist_ch['mpiprocs']
                while multiple > 0:
                    nec_dist_chunk = []
                    val = int(math.ceil(mpiprocs_in_select/nves_in_select))
                    times = int(mpiprocs_in_select/val)
                    last_value = mpiprocs_in_select%val
                    nec_dist_chunk.extend([str(val) for i in range(times)])
                    while times < nves_in_select:
                        nec_dist_chunk.append(str(last_value))
                        times += 1
                    nec_dist_str = ':'.join(nec_dist_chunk)
                    nec_dist.append(nec_dist_str)
                    multiple -= 1

        if not nec_dist:
            pbs.logmsg(pbs.EVENT_DEBUG, 'Unable to parse or '
                                        'construct NEC_PROCESS_DIST')
            return
        pbs.logmsg(pbs.EVENT_DEBUG, 'nec_dist = %s' % nec_dist)
        
        i = 0
        if len(nec_dist) < len(nodefile_dist):
            if len(nec_dist) == len(select_chunks):
                # do a 1-1 doubling
                # Example: lselect=2:mpiprocs=4:nves=3
                #          NEC_PROCESS_DIST=S2:1
                # then change nec_dist = [S2:1, S2:1]
                for chunk in select_chunks:
                    r = chunk.split(':')[0]
                    multi = 1
                    if r.isdigit():
                        multi = int(r)
                    while multi > 1:
                        val = nec_dist[i]
                        i += 1
                        nec_dist.insert(i, val)
                        multi -= 1
                    i += 1
            elif len(nec_dist) == 1:
                # repeat
                # Example: -lselect=2:mpiprocs=4:nves=2
                #          -v NEC_PROCESS_DIST=2
                # then change nec_dist = [2,2]
                val = nec_dist
                length = len(nec_dist)
                while length <= len(nodefile_dist):
                    nec_dist.extend(val)
                    length = len(nec_dist)
        if len(nec_dist) != len(nodefile_dist): 
            pbs.logmsg(pbs.EVENT_DEBUG, 'Cannot understand NEC_PROCESS_DIST')
            return
        pbs.logmsg(pbs.EVENT_DEBUG, 'updated nec_dist = %s' % nec_dist)
 
        # Update nodefile_dist with nec_process_dist
        i = 0
        for chunk in list(nodefile_dist):
            nodefile_dist[chunk]['nec_process_dist'] = nec_dist[i]
            i += 1
        pbs.logmsg(pbs.EVENT_DEBUG, 'nodefile_dist with NEC_PROCESS_DIST'
                                    ' = %s' % nodefile_dist)

        pbs.logmsg(pbs.EVENT_DEBUG, 'ves = %s' % ves)

        # filter execv for current host
        for chunk in list(nodefile_dist):
            if self.hostname not in nodefile_dist[chunk]['execv']:
                nodefile_dist.pop(chunk)

        # finally add corresponding ves to nodefile_dist
        dup_nodes = copy.deepcopy(nodes)
        for chunk in nodefile_dist:            
            ves_on_vnhost = []
            if "nves" not in nodefile_dist[chunk].keys():
                continue
            nves_req_in_chunk = int(nodefile_dist[chunk]['nves'])
            # since only pci vnodes will have ves assigned
            vnhost_pattern = r'%s\[[\d]+\].pci\d' % self.hostname
            # an execv can have more than one vnode name,
            # like in the case of super-chunking
            vnhost = re.findall(vnhost_pattern, nodefile_dist[chunk]['execv'])            
            nodefile_dist[chunk]['ves'] = []
            for vhost in vnhost:
                ves_on_vnhost.extend(dup_nodes[vhost]['ves'])
            for v in ves_on_vnhost:
                if v[-1] in ves:
                    nodefile_dist[chunk]['ves'].append(v[-1])
                    for vhost in vnhost:
                        if v in dup_nodes[vhost]['ves']:
                            dup_nodes[vhost]['ves'].remove(v)
                    nves_req_in_chunk -= 1
                    if nves_req_in_chunk == 0:
                        break

        pbs.logmsg(pbs.EVENT_DEBUG, 'final dict of nodefile_dist= %s'
                   % nodefile_dist)
        
        if ve_offloading == 1:              
            for line in nodefile_dist:
                nec_proc_dist = nodefile_dist[line]['nec_process_dist']
                for proc in nec_proc_dist:             
                    if '0' in proc:
                        offloading_ves.extend(nodefile_dist[line]['ves'])

        # write to file
        if pbs.pbs_conf['PBS_MOM_HOME']:
            home_dir = pbs.pbs_conf['PBS_MOM_HOME']
        else:
            home_dir = pbs.pbs_conf['PBS_HOME']
        path = os.path.join(home_dir, 'aux', self.job.id + '.ve_nodefile')
        file = open(path, "a")       
        
        for line in nodefile_dist:
            ve_val = []
            if 'ves' in nodefile_dist[line].keys():
                ve_val = nodefile_dist[line]['ves']
            each_env_val = nodefile_dist[line]['nec_process_dist'].split(':')

            # Check for ve offloading
            for env_val in each_env_val:
                if "0" == nec_procs_env:
                    ve_offloading = 1

            # identify cases like nves=2 and NEC_PROCESS_DIST is S2:3
            # make it S2:3:3
            len_each_env_val = len(each_env_val)
            non_host_val = ""
            for ele in each_env_val:
                if 's' in ele or 'S' in ele:
                    len_each_env_val -= 1
                else:
                    non_host_val = ele
            if len_each_env_val < len(ve_val):
                nec_st = nodefile_dist[line]['nec_process_dist'] 
                # case of 2:S2, make it 2:2:S2 for nves=2
                i = 0
                for ele in list(each_env_val):
                    if 's' in ele or 'S' in ele:
                        continue
                    else:
                        for c in range(int(
                                (len(ve_val) - len_each_env_val)
                                / len_each_env_val)):
                            each_env_val.insert(i+1, non_host_val)
                    i += 1
            pbs.logmsg(pbs.EVENT_DEBUG, 'updated each_env_val = %s'
                       % each_env_val)

            # check if env val add up to mpiprocs total
            # Example: -lselect=2:mpiprocs=3:nves=2 -v NEC_PROCESS_DIST=2
            # NEC_PROCESS_DIST should be [2:1]
            total_env_val = 0
            mpiprocs_total = 0
            for ele in each_env_val:
                if 's' in ele or 'S' in ele:
                    ele = ele[-1]
                total_env_val += int(ele)
            pbs.logmsg(pbs.EVENT_DEBUG, 'total_env_val = %s' % total_env_val)
            mpiprocs_total += int(nodefile_dist[line]['mpiprocs'])
            pbs.logmsg(pbs.EVENT_DEBUG, 'mpiprocs_total = %s' %
                       mpiprocs_total)
            diff = total_env_val - mpiprocs_total
            if diff != 0:
                # just update the last element
                each_env_val[-1] = str(int(each_env_val[-1]) - diff)
            pbs.logmsg(pbs.EVENT_DEBUG, 'updated after mpiprocs check: '
                                        'each_env_val = %s' % each_env_val)

            i = 0
            for ele in each_env_val:
                if 's' in ele or 'S' in ele:
                    for c in range(int(ele[-1])):
                        file.write("\n")
                else:
                    for c in range(int(ele)):
                        if not ve_val:
                            continue
                        file.write("ve=" + ve_val[i] + "\n")
                    i += 1
        return path

    def _write_job_env_file(self, ves, node, path, nqsv_flag, hca_dist=""):
        """
        Write out host NEC environment for this job
        """
        pbs.logmsg(pbs.EVENT_DEBUG4, '%s: Method called' % caller_name())
        env_list = []
        env_list.append('VE_NODE_NUMBER=%s' % ves[0])
        env_list.append('_VENODELIST=%s' % ' '.join(ves))
        pbs.logmsg(pbs.EVENT_DEBUG, "offloading_ves = %s" % offloading_ves)
        s = set(offloading_ves)
        ves_for_mpi_env = [x for x in ves if x not in s]
        if not ves_for_mpi_env and not ve_offloading:
            ves_for_mpi_env = ves
        env_list.append('_NECMPI_VE_NUM_NODES=%s' % len(ves_for_mpi_env))
        env_list.append('_NECMPI_VE_NODELIST=%s' % ' ' .join(ves_for_mpi_env))
        if nqsv_flag:
            env_list.append('_NEC_NQSV_JOB=1')
        env_list.append('PBS_NODEFILE_VE=%s' % path)

        hca_num, vnode_names = self._check_for_nhca_req(node)
        infini_env = ""

        if hca_num == "1":
            for ve in ves:
                for vnode in vnode_names:
                    if node[vnode]["nves"]:
                        if "VE" + ve in node[vnode]["ves"]:
                            hca = node[vnode]["ibs"][0]
                            infini_env += hca + hca_dist[hca] + " "
        else:
            infini = ""
            for hca in hca_dist:
                infini += hca + hca_dist[hca] + "\,"
            for i in range(len(ves)):
                infini_env += infini[:-2] + " "
        infini_env = infini_env[:-1]
        env_list.append('_NEC_HCA_LIST_IO=%s' % infini_env)
        if ve_offloading:
            infini_env = ""
            for i in range(len(ves) - len(offloading_ves)):
                infini_env += infini[:-2] + " "
        env_list.append('_NEC_HCA_LIST_MPI=%s' % infini_env)

        pbs.logmsg(pbs.EVENT_DEBUG, "%s" % env_list)
        jobid = str(e.job.id)
        if not os.path.exists(self.host_job_env_dir):
            os.makedirs(self.host_job_env_dir, 0o755)
        # Write out assigned_resources
        try:
            lines = "\n".join(env_list)
            filename = self.host_job_env_filename % jobid
            with open(filename, 'w') as desc:
                desc.write(lines)
            pbs.logmsg(pbs.EVENT_DEBUG4, 'Wrote out file: %s' % filename)
            pbs.logmsg(pbs.EVENT_DEBUG4, 'Data: %s' % lines)
            return True
        except Exception as exc:
            pbs.logmsg(pbs.EVENT_DEBUG, '%s: Failed to write to %s: %s' %
                       (caller_name(), filename, exc))
            e.reject("Failed to write to %s" % filename)

    def _check_for_nhca_req(self, nodes):
        """
        Method to check if nhcas is requested in first chunk of this host
        """
        pbs.logmsg(pbs.EVENT_DEBUG, '%s: Method called' % caller_name())

        chunks = str(self.job.schedselect).split("+")
        execv = re.findall(r'\(.*?\)', str(self.job.exec_vnode))
        host_execv = []

        # We need to get exec_vnodes of this host. We find out the VE's
        # of the vnodes and export the closest HCA.
        chunk_len = 0
        first_chunk = 0  # Index of the first select chunk on this host
        vnode_num = 0  # Number of actual vnodes (if -lplace=excl is asked)

        while chunk_len < len(execv):
            # If -lplace=excl is requested, do not consider
            # the additional vnodes, hence adding a check for
            # "ncpus"
            if "ncpus" in execv[chunk_len]:
                vnode_num += 1

                # Find out vnodes on this host
                if self.hostname in execv[chunk_len]:
                    if first_chunk == 0:
                        first_chunk = vnode_num
                    ch = execv[chunk_len].split("+")
                    for c in ch:
                        vnode_name = (c.split(":")[0]).strip('(').strip(')')
                        if vnode_name not in host_execv:
                            host_execv.append(vnode_name)
            chunk_len += 1

        pbs.logmsg(pbs.EVENT_DEBUG, "Host exec_vnode - %s" % host_execv)

        # Initialize HCA's to default i.e. 2
        hca_num = 2

        # Check if IB devices are connected to any vnodes.
        # If they are not connected, then export all the HCA's

        ib_node_len = 0
        for node, info in nodes.items():
            if "pci" in info:
                for key, val in info["pci"].items():
                    if "ibs" not in val:
                        ib_node_len += 1
                break
            else:
                continue

        if ib_node_len == len(nodes):
            pbs.logmsg(pbs.EVENT_DEBUG, "Vnodes does not have IB "
                                        "devices attached")
            pbs.logmsg(pbs.EVENT_DEBUG, "Number of HCA's - %s" % hca_num)
            return hca_num, host_execv

        # Find the value of hcas requested in the first chunk
        # If nhcas is nott requested, then we export all.
        chunk_len = 0
        chunk_index = 0
        temp = 0
        while chunk_len < len(chunks):
            chunk = chunks[chunk_len].split(":")
            if chunk[0].isdigit():
                chunk_index += int(chunk[0])
            else:
                chunk_index += 1

            if temp < first_chunk <= chunk_index:
                pbs.logmsg(pbs.EVENT_DEBUG, "First chunk on this node - %s"
                           % chunks[chunk_len])
                for resc in chunk:
                    if "nhcas" in resc:
                        hca_num = resc.split("=")[1]
                break
            temp = chunk_index
            chunk_len += 1
        pbs.logmsg(pbs.EVENT_DEBUG, "Number of HCA's - %s" % hca_num)
        return hca_num, host_execv

    def _assign_ves(self, nodes, assigned_ves):
        """
        Assign VEs for the job
        """
        pbs.logmsg(pbs.EVENT_DEBUG, '%s: Method called' % caller_name())

        ves_available = {}
        assigned_res = self.assigned_resources
        for vnode in assigned_res['vnodes']:
            if 'nves' in assigned_res['vnodes'][vnode].keys():
                ves_available[vnode] = []
                try:
                    for ve in list(nodes[vnode]['ves']):
                        if ve[-1] in assigned_ves:
                            nodes[vnode]['ves'].remove(ve)
                    ves_available[vnode].extend(nodes[vnode]['ves'])
                except KeyError:
                    msg = "%s might have all its VEs in Offline state" % vnode
                    e.reject(msg)

        # Find the number of ves requested and assign it
        ves_req = []
        for vnode in assigned_res['vnodes']:
            if 'nves' in assigned_res['vnodes'][vnode].keys():
                ves_req.append(assigned_res['vnodes'][vnode]['nves'])
        i = 0
        ve_num = []
        for ve_node in ves_available:
            for ve in ves_available[ve_node][:ves_req[i]]:
                ve_num.append(ve[-1])
            i += 1
        # do the following check in case some VEs are not in ONLINE state
        if sum(ves_req) != len(ve_num):
            msg = "some VE's are not online. Rejecting job for a requeue"
            e.reject(msg)
        pbs.logmsg(pbs.EVENT_DEBUG, 'Assigned ves = %s' % ve_num)
        ve_num.sort()

        return ve_num

    def setup_job_devices_env(self):
        """
        Retrieve and setup the job environment for VEs assigned
        to the job for an execjob_launch hook
        """
        pbs.logmsg(pbs.EVENT_DEBUG, '%s: Method called' % caller_name())
        filename = self.host_job_env_filename % e.job.id
        if not os.path.isfile(filename):
            return
        try:
            with open(filename, 'r') as f1:
                lines = f1.readlines()
        except IOError:
            pbs.logmsg(pbs.EVENT_DEBUG, 'Failed to open file: %s' %
                       filename)
            e.reject("IOError while trying to open file: %s" % filename)
            
        for line in lines:
            key = line.split('=')[0]
            value = line.split('=')[1].strip("\n")
            e.env[key] = '%s' % value

    def _get_assigned_job_resources(self, hostname=None):
        """
        Return a dictionary of assigned resources on the local node
        """
        pbs.logmsg(pbs.EVENT_DEBUG4, '%s: Method called' % caller_name())
        # Bail out if no hostname was provided
        if not hostname:
            hostname = self.hostname
        if not hostname:
            raise ProcessingError('No hostname available')
        # Bail out if no job information is present
        if self.job == None:
            raise ProcessingError('No job information available')
        # Create a list of local vnodes
        vnodes = []
        vnhost_pattern = r'%s\[[\d]+\].pci\d' % hostname
        pbs.logmsg(pbs.EVENT_DEBUG, '%s: vnhost pattern: %s' %
                   (caller_name(), vnhost_pattern))
        pbs.logmsg(pbs.EVENT_DEBUG, '%s: Job exec_vnode list: %s' %
                   (caller_name(), self.job.exec_vnode))
        for match in re.findall(vnhost_pattern, str(self.job.exec_vnode)):
            vnodes.append(match)
        if vnodes:
            pbs.logmsg(pbs.EVENT_DEBUG, '%s: Vnodes on %s: %s' %
                       (caller_name(), hostname, vnodes))
        # Collect host assigned resources
        resources = {}
        for chunk in self.job.exec_vnode.chunks:
            if vnodes:
                # Vnodes list is not empty
                if chunk.vnode_name not in vnodes:
                    continue
                if 'vnodes' not in resources:
                    resources['vnodes'] = {}
                if chunk.vnode_name not in resources['vnodes']:
                    resources['vnodes'][chunk.vnode_name] = {}
                # Initialize any missing resources for the vnode.
                # This check is needed because some resources might
                # not be present in each chunk of a job. For example:
                # exec_vnodes =
                # (node1[0]:ncpus=4:mem=4gb+node1[1]:mem=2gb) +
                # (node1[1]:ncpus=3+node[0]:ncpus=1:mem=4gb)
                for resc in list(chunk.chunk_resources.keys()):
                    vnresc = resources['vnodes'][chunk.vnode_name]
                    if resc in list(vnresc.keys()):
                        pbs.logmsg(pbs.EVENT_DEBUG, '%s: %s:%s defined' %
                                   (caller_name(), chunk.vnode_name, resc))
                    else:
                        pbs.logmsg(pbs.EVENT_DEBUG, '%s: %s:%s missing' %
                                   (caller_name(), chunk.vnode_name, resc))
                        vnresc[resc] = \
                            initialize_resource(chunk.chunk_resources[resc])
                pbs.logmsg(pbs.EVENT_DEBUG, '%s: Chunk %s resources: %s' %
                           (caller_name(), chunk.vnode_name, resources))
            else:
                # Vnodes list is empty
                if chunk.vnode_name != hostname:
                    continue
            for resc in list(chunk.chunk_resources.keys()):
                if resc not in list(resources.keys()):
                    resources[resc] = \
                        initialize_resource(chunk.chunk_resources[resc])
                # Add resource value to total
                if isinstance(chunk.chunk_resources[resc],
                              (pbs.pbs_int, pbs.pbs_float, pbs.size)):
                    resources[resc] += chunk.chunk_resources[resc]
                    pbs.logmsg(pbs.EVENT_DEBUG,
                               '%s: resources[%s][%s] is now %s' %
                               (caller_name(), hostname, resc,
                                resources[resc]))
                    if vnodes:
                        resources['vnodes'][chunk.vnode_name][resc] += \
                            chunk.chunk_resources[resc]
                else:
                    pbs.logmsg(pbs.EVENT_DEBUG,
                               '%s: Setting resource %s to string %s' %
                               (caller_name(), resc,
                                str(chunk.chunk_resources[resc])))
                    resources[resc] = str(chunk.chunk_resources[resc])
                    if vnodes:
                        resources['vnodes'][chunk.vnode_name][resc] = \
                            str(chunk.chunk_resources[resc])
        if resources:
            pbs.logmsg(pbs.EVENT_DEBUG, '%s: Resources for %s: %s' %
                       (caller_name(), hostname, repr(resources)))
            # Return assigned resources for specified host
            return resources
        else:
            pbs.logmsg(pbs.EVENT_JOB_USAGE, "WARNING: job seems "
                       + "to have no resources assigned to this host.")
            pbs.logmsg(pbs.EVENT_JOB_USAGE,
                       "Server and MoM vnode names may not be consistent.")
            pbs.logmsg(pbs.EVENT_JOB_USAGE,
                       "Pattern for expected vnode name(s) is %s"
                       % vnhost_pattern)
            pbs.logmsg(pbs.EVENT_JOB_USAGE,
                       "Job exec_vnode is %s" % str(self.job.exec_vnode))
            pbs.logmsg(pbs.EVENT_JOB_USAGE,
                       "You may have forgotten to set PBS_MOM_NODE_NAME to "
                       "the desired matching entry in the exec_vnode string")
            pbs.logmsg(pbs.EVENT_JOB_USAGE,
                       "Job will fail or be configured with default "
                       "ncpus/mem")
            return {}

    def get_parent_pid(self):
        """
        Helper method for fetching the parent pid
        of the job on this node. 
        """
        if self.job.in_ms_mom():
            # If the node is mother superior, session id
            # is the mother superior
            return str(self.job.session_id)
        else:
            # If the node is sister, read the pid from the parent pid file
            # i.e. created in execjob_launch hook event for this job. 
            pid = ""
            try:
                fn = self.host_job_ppid % self.job.id
                with open(fn, 'r') as desc:
                    pid = desc.read()
            except Exception as exc:
                return 
            return pid
         
    def get_child_pids(self):
        """
        Helper method for fetching the child pids
        of a job on this node.
        """

        ps_cmd = ""
        parent_pid = self.get_parent_pid()
        if not parent_pid:
            return 

        ps_cmd = ["ps", "-s", parent_pid, "-o", "pid", "--no-headers"]
        return exec_cmd(ps_cmd)


#
# CLASS NodeUtils
#
class NodeUtils(object):
    """
    Node utility methods
    NOTE: Multiple log messages pertaining to devices have been commented
          out due to the size of the messages. They may be uncommented for
          additional debugging if necessary.
    """

    def __init__(self, hostname=None, cpuinfo=None, meminfo=None,
                 numa_nodes=None, ves=None, ibs=None):

        self.nodes = {}
        if hostname != None:
            self.hostname = hostname
        else:
            self.hostname = pbs.get_local_nodename()
        if cpuinfo != None:
            self.cpuinfo = cpuinfo
        else:
            self.cpuinfo = self._discover_cpuinfo()
        if meminfo != None:
            self.meminfo = meminfo
        else:
            self.meminfo = self._discover_meminfo()
        if numa_nodes != None:
            self.numa_nodes = numa_nodes
        else:
            self.numa_nodes = dict()
            self.numa_nodes = self._discover_numa_nodes()
       
        # Update the Numa node dictionary with VE devices
        # and IB devices info

        devices = {"ves": ves, "ibs": ibs}
        self._update_numa_nodes_with_devices(devices)

        pbs.logmsg(pbs.EVENT_DEBUG4, "Updated Numa nodes info: %s"
                   % self.numa_nodes)
 
        self._finalize_nodes()

        # Add the ve device count to the nodes
        self._add_ve_device_counts_to_nodes()

    def _update_numa_nodes_with_devices(self, devices):
        """
        Helper method to update the Numa nodes
        dictionary with VE and IB devices and its associated
        PCIe.
        """
        for dtype, dinfo in devices.items():
            for device, info in dinfo.items():
                dtname = copy.copy(dtype)
                dname = copy.copy(device)

                # Identify PCI_bus_id from the ve info
                pci_bus_id = info["pci"]

                # The if-block is for the model in which
                # each numa node has multiple pcie slots.
                # The else-block is for a model in which each numa node
                # has one pcie slot.

                if len(pci_bus_id) > 0:
                    # If there are any pcies attached to this numa node
                    # update numa nodes dictionary with the pci bus id
                    if "pci" not in self.numa_nodes[info["numa_node"]]:
                        self.numa_nodes[info["numa_node"]]["pci"] = {}
                    if pci_bus_id not in \
                            self.numa_nodes[info["numa_node"]]["pci"]:
                        self.numa_nodes[info["numa_node"]][
                            "pci"][pci_bus_id] = {}
                    if dtname not in \
                            self.numa_nodes[
                                info["numa_node"]]["pci"][pci_bus_id]:
                        self.numa_nodes[
                            info["numa_node"]]["pci"][pci_bus_id][dtname] = []
                    self.numa_nodes[info["numa_node"]][
                        "pci"][pci_bus_id][dtname].append(dname)
                else:
                    if dtname not in self.numa_nodes[info["numa_node"]]:
                        self.numa_nodes[info["numa_node"]][dtname] = []
                    self.numa_nodes[info["numa_node"]][dtname].append(dname)

    def _finalize_nodes(self):
        """
        Determine the pci bus ids attached to each numa node
        and create a final list of the nodes 
        """
        pbs.logmsg(pbs.EVENT_DEBUG4, '%s: Method called' % caller_name())
        for nnid in self.numa_nodes.keys():
            pci_num = 0
            if "pci" in self.numa_nodes[nnid].keys():
                for pci, info in self.numa_nodes[nnid]["pci"].items():
                    node_name = self.hostname + '[%d]_pci%d' % (nnid, pci_num)
                    self.nodes[node_name] = \
                        copy.deepcopy(self.numa_nodes[nnid])
                    self.nodes[node_name]["numa_node"] = [nnid]
                    try:
                        self.nodes[node_name]["ves"] = copy.deepcopy(info["ves"])
                    except KeyError:
                        # "ves" must be empty because all of them are Offline
                        pass
                    if "ibs" in info:
                        self.nodes[node_name]["ibs"] = \
                            copy.deepcopy(info["ibs"])
                    pci_num += 1
            else:
                if "ves" in self.numa_nodes[nnid].keys():
                    node_name = self.hostname + '[%d]_pci%d' % (nnid, pci_num)
                    self.nodes[node_name] = \
                        copy.deepcopy(self.numa_nodes[nnid])
                    self.nodes[node_name]["numa_node"] = [nnid]
                    self.nodes[node_name]["ves"] = \
                        copy.deepcopy(self.numa_nodes[nnid]["ves"])
                    if "ibs" in self.numa_nodes[nnid].keys():
                        self.nodes[node_name]["ibs"] = \
                            copy.deepcopy(self.numa_nodes[nnid]["ibs"])
                else:
                    node_name = self.hostname + '[%d]' % nnid
                    self.nodes[node_name] = \
                        copy.deepcopy(self.numa_nodes[nnid])
                pci_num += 1
        pbs.logmsg(pbs.LOG_DEBUG, "Final nodes info: %s" % self.nodes)

    def _add_ve_device_counts_to_nodes(self):
        """
        Update the device counts per numa node
        """
        pbs.logmsg(pbs.EVENT_DEBUG4, '%s: Method called' % caller_name())
        nodes = copy.deepcopy(self.nodes)

        for node, info in nodes.items():
            if "ves" in info:
                self.nodes[node]["nves"] = len(info["ves"])
            else:
                self.nodes[node]["nves"] = 0
        pbs.logmsg(pbs.EVENT_DEBUG4, 'Nodes: %s' % self.nodes)
        return

    def _discover_numa_nodes(self):
        """
        Discover what type of hardware is on this node and how it
        is partitioned
        """
        pbs.logmsg(pbs.EVENT_DEBUG4, '%s: Method called' % caller_name())
        numa_nodes = {}
        for node in glob.glob(os.path.join(os.sep, 'sys', 'devices',
                                           'system', 'node', 'node*')):
            # The basename will be node0, node1, etc.
            # Capture the numeric portion as the identifier/ordinal.
            num = int(os.path.basename(node)[4:])
            if num not in numa_nodes:
                numa_nodes[num] = {}
            with open(os.path.join(node, 'cpulist'), 'r') as desc:
                avail = expand_list(desc.readline())
                numa_nodes[num]['cpus'] = [x for x in avail]
            with open(os.path.join(node, 'meminfo'), 'r') as desc:
                for line in desc:
                    # Each line will contain four or five fields. Examples:
                    # Node 0 MemTotal:       32995028 kB
                    # Node 0 HugePages_Total:     0
                    entries = line.split()
                    if len(entries) < 4:
                        continue
                    if entries[2] == 'MemTotal:':
                        numa_nodes[num]['MemTotal'] = \
                            convert_size(entries[3] + entries[4], 'k')
                    elif entries[2] == 'HugePages_Total:':
                        numa_nodes[num]['HugePages_Total'] = int(entries[3])

        # Update the numa nodes dictionary with mem, vmem and hpmem values
        num_numa_nodes = len(numa_nodes)
        if num_numa_nodes > 0:
            # Physical Memory 
            host_mem = self.get_memory_on_node()

            # Swap to be added to virtual memory net values
            host_vmem = self.get_vmem_on_node()
            node_swapmem = int(math.floor((host_vmem - host_mem)
                                          / num_numa_nodes))
            if node_swapmem < 0:
                node_swapmem = 0
            node_swapmem -= node_swapmem % (1024 * 1024)
            for num in numa_nodes:
                val = 0
                val = numa_nodes[num]['HugePages_Total']
                if 'Hugepagesize' in self.meminfo:
                    val *= size_as_int(self.meminfo['Hugepagesize'])
                numa_nodes[num]['hpmem'] = val
                val = size_as_int(numa_nodes[num]['MemTotal'])
                numa_nodes[num]['mem'] = val
                val += node_swapmem
                # round down only svr-reported values, not internal values
                numa_nodes[num]['vmem'] = val
        pbs.logmsg(pbs.EVENT_DEBUG4, '%s: %s' % (caller_name(), numa_nodes))
        return numa_nodes

    def _discover_meminfo(self):
        """
        Return a dictionary where the keys are the NUMA node ordinals
        and the values are the various memory sizes
        """
        pbs.logmsg(pbs.EVENT_DEBUG4, '%s: Method called' % caller_name())
        meminfo = {}
        with open(os.path.join(os.sep, 'proc', 'meminfo'), 'r') as desc:
            for line in desc:
                entries = line.split()
                if entries[0] == 'MemTotal:':
                    meminfo[entries[0].rstrip(':')] = \
                        convert_size(entries[1] + entries[2], 'k')
                elif entries[0] == 'SwapTotal:':
                    meminfo[entries[0].rstrip(':')] = \
                        convert_size(entries[1] + entries[2], 'k')
                elif entries[0] == 'Hugepagesize:':
                    meminfo[entries[0].rstrip(':')] = \
                        convert_size(entries[1] + entries[2], 'k')
                elif entries[0] == 'HugePages_Total:':
                    meminfo[entries[0].rstrip(':')] = int(entries[1])
                elif entries[0] == 'HugePages_Rsvd:':
                    meminfo[entries[0].rstrip(':')] = int(entries[1])
        pbs.logmsg(pbs.EVENT_DEBUG4, 'Discover meminfo: %s' % meminfo)
        return meminfo

    def _discover_cpuinfo(self):
        """
        Return a dictionary where the keys include both global settings
        and individual CPU characteristics
        """
        pbs.logmsg(pbs.EVENT_DEBUG4, '%s: Method called' % caller_name())
        cpuinfo = {}
        cpuinfo['cpu'] = {}
        proc = None
        with open(os.path.join(os.sep, 'proc', 'cpuinfo'), 'r') as desc:
            for line in desc:
                entries = line.strip().split(':')
                if len(entries) < 2:
                    # Blank line indicates end of processor
                    proc = None
                    continue
                key = entries[0].strip()
                val = entries[1].strip()
                if proc == None and key != 'processor':
                    raise ProcessingError('Failed to parse /proc/cpuinfo')
                if key == 'processor':
                    proc = int(val)
                    if proc in cpuinfo:
                        raise ProcessingError('Duplicate CPU ID found')
                    cpuinfo['cpu'][proc] = {}
                    cpuinfo['cpu'][proc]['threads'] = []
                elif key == 'flags':
                    cpuinfo['cpu'][proc][key] = val.split()
                elif val.isdigit():
                    cpuinfo['cpu'][proc][key] = int(val)
                else:
                    cpuinfo['cpu'][proc][key] = val
        if not cpuinfo['cpu']:
            raise ProcessingError('No CPU information found')
        cpuinfo['logical_cpus'] = len(cpuinfo['cpu'])
        cpuinfo['hyperthreads_per_core'] = 1
        cpuinfo['hyperthreads'] = []
        # Now try to construct a dictionary with hyperthread information
        # if this is an Intel based processor
        try:
            if ('Intel' in cpuinfo['cpu'][0]['vendor_id']
                    or 'AuthenticAMD' in cpuinfo['cpu'][0]['vendor_id']):
                if 'ht' in cpuinfo['cpu'][0]['flags']:
                    cpuinfo['hyperthreads_per_core'] = \
                        int(cpuinfo['cpu'][0]['siblings']
                            // cpuinfo['cpu'][0]['cpu cores'])
                    # Map hyperthreads to physical cores
                    if cpuinfo['hyperthreads_per_core'] > 1:
                        pbs.logmsg(pbs.EVENT_DEBUG4,
                                   'Mapping hyperthreads to cores')
                        cores = list(cpuinfo['cpu'].keys())
                        threads = set()
                        # CPUs with matching core IDs are hyperthreads
                        # sharing the same physical core. Loop through
                        # the cores to construct a list of threads.
                        for xid in cores:
                            xcore = cpuinfo['cpu'][xid]
                            for yid in cores:
                                if yid < xid:
                                    continue
                                if yid == xid:
                                    cpuinfo['cpu'][xid]['threads'].append(yid)
                                    continue
                                ycore = cpuinfo['cpu'][yid]
                                if xcore['physical id'] != \
                                        ycore['physical id']:
                                    continue
                                if xcore['core id'] == ycore['core id']:
                                    cpuinfo['cpu'][xid]['threads'].append(yid)
                                    cpuinfo['cpu'][yid]['threads'].append(xid)
                                    threads.add(yid)
                        pbs.logmsg(pbs.EVENT_DEBUG4, 'HT cores: %s' % threads)
                        cpuinfo['hyperthreads'] = sorted(threads)
                    else:
                        cores = cpuinfo['cpu'].keys()
                        for xid in cores:
                            cpuinfo['cpu'][xid]['threads'].append(xid)
        except Exception:
            pbs.logmsg(pbs.EVENT_DEBUG, '%s: Hyperthreading check failed' %
                       caller_name())
        cpuinfo['physical_cpus'] = int(cpuinfo['logical_cpus']
                                       // cpuinfo['hyperthreads_per_core'])
        pbs.logmsg(pbs.EVENT_DEBUG4, '%s returning: %s' %
                   (caller_name(), cpuinfo))
        return cpuinfo

    def get_memory_on_node(self, memtotal=None):
        """
        Get the memory resource on this mom
        """
        pbs.logmsg(pbs.EVENT_DEBUG4, '%s: Method called' % caller_name())
        total = 0
        if self.numa_nodes:
            # Caller wants the sum of all NUMA nodes
            for nnid in self.numa_nodes:
                if 'mem' in self.numa_nodes[nnid]:
                    total += self.numa_nodes[nnid]['mem']
                else:
                    # NUMA node unreliable, make sure other method is used
                    total = 0
                    break
            # only round down svr-reported values, not internal values
            if total > 0:
                return total
            pbs.logmsg(pbs.EVENT_DEBUG4,
                       '%s: Failed to obtain memory using NUMA node method' %
                       caller_name())
        # Calculate total memory
        try:
            if memtotal == None:
                total = size_as_int(self.meminfo['MemTotal'])
            else:
                total = size_as_int(memtotal)
        except Exception:
            pbs.logmsg(pbs.EVENT_DEBUG,
                       '%s: Could not determine total node memory' %
                       caller_name())
            raise
        if total <= 0:
            raise ValueError('Total node memory value invalid')
        pbs.logmsg(pbs.EVENT_DEBUG4, 'total visible mem: %d' % total)
        amount = convert_size(str(total), 'k')
        pbs.logmsg(pbs.EVENT_DEBUG4, '%s: Returning: %s' %
                   (caller_name(), amount))
        return size_as_int(total)

    def get_vmem_on_node(self, vmemtotal=None):
        """
        Get the virtual memory resource on this mom
        """
        pbs.logmsg(pbs.EVENT_DEBUG4, '%s: Method called' % caller_name())
        total = 0
        # If NUMA nodes were not yet discovered then get totals
        # using non-NUMA methods
        if self.numa_nodes:
            # Caller wants the sum of all NUMA nodes, and they were
            # computed earlier
            for nnid in self.numa_nodes:
                if 'vmem' in self.numa_nodes[nnid]:
                    total += self.numa_nodes[nnid]['vmem']
                else:
                    total = 0
                    break
            # only round down svr-reported values, not internal values
            if total > 0:
                return total
            pbs.logmsg(pbs.EVENT_DEBUG4,
                       '%s: Failed to obtain vmem using NUMA node method' %
                       caller_name())
        # Calculate total vmem; start with visible or usable physical memory
        total = self.get_memory_on_node(None)
        pbs.logmsg(pbs.EVENT_DEBUG4, 'total visible mem: %d' % total)

        # Calculate total swap
        try:
            if vmemtotal == None:
                swap = size_as_int(self.meminfo['SwapTotal'])
            else:
                swap = size_as_int(vmemtotal)
        except Exception:
            pbs.logmsg(pbs.EVENT_DEBUG,
                       '%s: Could not determine total node swap' %
                       caller_name())
            raise
        if swap <= 0:
            pbs.logmsg(pbs.EVENT_DEBUG4,
                       '%s: No swap space detected' %
                       caller_name())
            swap = 0
        pbs.logmsg(pbs.EVENT_DEBUG4, 'total swap: %d' % swap)
        total += swap
        
        pbs.logmsg(pbs.EVENT_DEBUG4, 'total vmem: %d' % total)
        amount = convert_size(str(total), 'k')
        pbs.logmsg(pbs.EVENT_DEBUG4, '%s: Returning: %s' %
                   (caller_name(), amount))
        return size_as_int(total)

    def get_hpmem_on_node(self, hpmemtotal=None):
        """
        Get the huge page memory resource on this mom
        """
        pbs.logmsg(pbs.EVENT_DEBUG4, '%s: Method called' % caller_name())
        total = 0
        if self.numa_nodes:
            # Caller wants the sum of all NUMA nodes
            for nnid in self.numa_nodes:
                if 'hpmem' in self.numa_nodes[nnid]:
                    total += self.numa_nodes[nnid]['hpmem']
                else:
                    total = 0
                    break
            # only round down svr-reported values, not internal values
            if total > 0:
                return total
            pbs.logmsg(pbs.EVENT_DEBUG4,
                       '%s: Failed to obtain memory using NUMA node method' %
                       caller_name())
        # Calculate hpmem
        try:
            if hpmemtotal == None:
                total = size_as_int(self.meminfo['Hugepagesize'])
                total *= (self.meminfo['HugePages_Total'] -
                          self.meminfo['HugePages_Rsvd'])
            else:
                total = size_as_int(hpmemtotal)
        except Exception:
            pbs.logmsg(pbs.EVENT_DEBUG3,
                       '%s: Could not determine huge page availability' %
                       caller_name())
            total = 0
        if total <= 0:
            total = 0
            pbs.logmsg(pbs.EVENT_DEBUG4,
                       '%s: No huge page memory detected' %
                       caller_name())
            return 0
        amount = convert_size(str(total), 'k')
        pbs.logmsg(pbs.EVENT_DEBUG4, '%s: Returning: %s' %
                   (caller_name(), amount))
        # Remove any bytes beyond the last MB
        return size_as_int(total)

    def create_vnodes(self):
        """
        Create vnodes for each PCIe with ves and each numa node
        which does not have any ve.
        """
        pbs.logmsg(pbs.EVENT_DEBUG4, '%s: Method called' % caller_name())
        vnode_list = e.vnode_list
        pbs.logmsg(pbs.EVENT_DEBUG4, '%s: Nodes: %s' %
                   (caller_name(), self.nodes))
        vnode_name = self.hostname
        # In some cases the hostname and vnode name do not match
        # admin should fix this!
        # Give hints
        if vnode_name not in vnode_list:
            pbs.logmsg(pbs.EVENT_ERROR,
                       "Could not find hostname %s in vnode_list %s"
                       % (vnode_name, str(vnode_list.keys())))
            pbs.logmsg(pbs.EVENT_ERROR,
                       "This error is FATAL. Possible causes:")
            pbs.logmsg(pbs.EVENT_ERROR, "a) the server's name for "
                       "the natural node created on the server "
                       "does not match the output of 'hostname' on the host.")
            pbs.logmsg(pbs.EVENT_ERROR, "   Please use PBS_MOM_NODE_NAME "
                       "in /etc/pbs.conf to tell MoM the correct vnode name.")
            pbs.logmsg(pbs.EVENT_ERROR, "b) v2 config files are used "
                       "but none mention the natural vnode. Add a line for "
                       "the natural vnode in one of the v2 config files.")
            raise ProcessingError('Could not identify local vnode')
        vnode_list[vnode_name] = pbs.vnode(vnode_name)
        host_resc_avail = vnode_list[vnode_name].resources_available
        pbs.logmsg(pbs.EVENT_DEBUG4, '%s: host_resc_avail: %s' %
                   (caller_name(), host_resc_avail))
        
        vnode_msg_cpu = '%s: vnode_list[%s].resources_available[ncpus] = %d'
        vnode_msg_mem = '%s: vnode_list[%s].resources_available[mem] = %s'

        # set the value on the host to 0
        host_resc_avail['mem'] = pbs.size('0')
        host_resc_avail['vmem'] = pbs.size('0')
        host_resc_avail['hpmem'] = pbs.size('0')

        for node, info in self.nodes.items():     
            vnode_key = node
            vnode_list[vnode_key] = pbs.vnode(vnode_name)
            vnode_resc_avail = vnode_list[vnode_key].resources_available
            for key, val in info.items():
                if key == None:
                    pbs.logmsg(pbs.EVENT_DEBUG4, '%s: key is None'
                               % caller_name())
                    continue
                if val == None:
                    pbs.logmsg(pbs.EVENT_DEBUG4, '%s: val is None'
                               % caller_name())
                    continue
                pbs.logmsg(pbs.EVENT_DEBUG4, '%s: %s = %s'
                           % (caller_name(), key, val))
                if key in ['MemTotal', 'HugePages_Total']:
                    # Irrelevant: transformed to other keys if vnodes is True
                    # done outside of loop if vnodes is False
                    pbs.logmsg(pbs.EVENT_DEBUG4, '%s: key %s skipped'
                               % (caller_name(), key))
                elif key == 'cpus':
                    if "pci" in info:
                        threads = len(val) / len(info["pci"]) 
                    else:
                        threads = len(val)

                    if "nves" in info and info["nves"] > 0:
                        threads -= info["nves"]
                    # set the value on the host to 0
                    host_resc_avail['ncpus'] = 0
                    pbs.logmsg(pbs.EVENT_DEBUG4, vnode_msg_cpu %
                               (caller_name(), vnode_name,
                                host_resc_avail['ncpus']))
                    vnode_resc_avail['ncpus'] = threads
                    pbs.logmsg(pbs.EVENT_DEBUG4, vnode_msg_cpu %
                               (caller_name(), vnode_key,
                                vnode_resc_avail['ncpus']))
                elif key in ['mem', 'vmem', 'hpmem']:
                    # Used for vnodes per NUMA socket
                    mem_val = val
                    if isinstance(val, float):
                        mem_val = int(val)
                    if "pci" in info:
                        mem_val = mem_val / len(info["pci"])
                    vnode_resc_avail[key] = pbs.size(convert_size(mem_val, 'm'))
                elif isinstance(val, list):
                    pass
                elif isinstance(val, dict):
                    pass
                else:
                    pbs.logmsg(pbs.EVENT_DEBUG4, '%s: key = %s (%s)' %
                               (caller_name(), key, type(key)))
                    pbs.logmsg(pbs.EVENT_DEBUG4, '%s: val = %s (%s)' %
                               (caller_name(), val, type(val)))
                    vnode_resc_avail[key] = val
                    host_resc_avail[key] = initialize_resource(val)
        
        pbs.logmsg(pbs.EVENT_DEBUG4, '%s: vnode list: %s' %
                   (caller_name(), str(vnode_list)))
        
        for node, info in self.nodes.items():
            vnode_key = node
            vnode_resc_avail = vnode_list[vnode_key].resources_available
            pbs.logmsg(pbs.EVENT_DEBUG4, '%s: %s vnode_resc_avail: %s' %
                       (caller_name(), vnode_key, vnode_resc_avail))
        pbs.logmsg(pbs.EVENT_DEBUG4, '%s: host_resc_avail: %s' %
                   (caller_name(), host_resc_avail))
        return True

    def update_vnodes(self):
        """
        Update vnodes of each PCIe with ves.
        """
        pbs.logmsg(pbs.EVENT_DEBUG4, '%s: Method called' % caller_name())
        vnode_list = e.vnode_list
        pbs.logmsg(pbs.EVENT_DEBUG4, '%s: Nodes: %s' %
                   (caller_name(), self.nodes))
        pbs.logmsg(pbs.EVENT_DEBUG4, '%s: vnode_list: %s' %
                   (caller_name(), vnode_list))
        for vnode in vnode_list:
            if vnode not in self.nodes.keys():
                # All the VEs of this vnode are offline.
                try:
                    if vnode_list[vnode].resources_available['nves'] != 0:
                        msg = "Updating resources_available.nves of %s" % vnode
                        msg += " from %s to 0" % vnode_list[vnode].resources_available['nves']
                        vnode_list[vnode].resources_available['nves'] = 0
                        pbs.logmsg(pbs.EVENT_DEBUG, '%s' % msg)
                    continue
                except AttributeError:
                    continue
            if self.nodes[vnode]['nves'] != vnode_list[vnode].resources_available['nves']:
                msg = "Updating resources_available.nves of %s" % vnode
                msg += " from %s to %s" % (vnode_list[vnode].resources_available['nves'],
                                        self.nodes[vnode]['nves'])
                vnode_list[vnode].resources_available['nves'] = self.nodes[vnode]['nves']
                pbs.logmsg(pbs.EVENT_DEBUG, '%s' % msg)


def set_global_vars():
    """
    Define some global variables that the hook may use
    """
    global PBS_EXEC
    global PBS_HOME
    global PBS_MOM_HOME
    global PBS_MOM_JOBS
    # Determine location of PBS_HOME, PBS_MOM_HOME, and PBS_EXEC. These
    # should have each be initialized to empty strings near the beginning
    # of this hook.
    # Try the environment first
    if not PBS_EXEC and 'PBS_EXEC' in os.environ:
        PBS_EXEC = os.environ['PBS_EXEC']
    if not PBS_HOME and 'PBS_HOME' in os.environ:
        PBS_HOME = os.environ['PBS_HOME']
    if not PBS_MOM_HOME and 'PBS_MOM_HOME' in os.environ:
        PBS_MOM_HOME = os.environ['PBS_MOM_HOME']
    # Try the built in config values next
    pbs_conf = pbs.get_pbs_conf()
    if pbs_conf:
        if not PBS_EXEC and 'PBS_EXEC' in pbs_conf:
            PBS_EXEC = pbs_conf['PBS_EXEC']
        if not PBS_HOME and 'PBS_HOME' in pbs_conf:
            PBS_HOME = pbs_conf['PBS_HOME']
        if not PBS_MOM_HOME and 'PBS_MOM_HOME' in pbs_conf:
            PBS_MOM_HOME = pbs_conf['PBS_MOM_HOME']
    # Try reading the config file directly
    if not PBS_EXEC or not PBS_HOME or not PBS_MOM_HOME:
        if 'PBS_CONF_FILE' in os.environ:
            pbs_conf_file = os.environ['PBS_CONF_FILE']
        else:
            pbs_conf_file = os.path.join(os.sep, 'etc', 'pbs.conf')
        regex = re.compile(r'\s*([^\s]+)\s*=\s*([^\s]+)\s*')
        try:
            with open(pbs_conf_file, 'r') as desc:
                for line in desc:
                    match = regex.match(line)
                    if match:
                        if not PBS_EXEC and match.group(1) == 'PBS_EXEC':
                            PBS_EXEC = match.group(2)
                        if not PBS_HOME and match.group(1) == 'PBS_HOME':
                            PBS_HOME = match.group(2)
                        if not PBS_MOM_HOME and (match.group(1) ==
                                                 'PBS_MOM_HOME'):
                            PBS_MOM_HOME = match.group(2)
        except Exception:
            pass
    # If PBS_MOM_HOME is not set, use the PBS_HOME value
    if not PBS_MOM_HOME:
        PBS_MOM_HOME = PBS_HOME
    PBS_MOM_JOBS = os.path.join(PBS_MOM_HOME, 'mom_priv', 'jobs')
    # Sanity check to make sure each global path is set
    if not PBS_EXEC:
        raise ConfigError('Unable to determine PBS_EXEC')
    if not PBS_HOME:
        raise ConfigError('Unable to determine PBS_HOME')
    if not PBS_MOM_HOME:
        raise ConfigError('Unable to determine PBS_MOM_HOME')


def main():
    """
    Main function for execution
    """
    pbs.logmsg(pbs.EVENT_DEBUG4, '%s: Function called' % caller_name())
    # If vecmd command not present, skip the hook execution
    if not os.path.exists('/opt/nec/ve/bin/vecmd'):
        pbs.logmsg(pbs.EVENT_DEBUG,
                   '%s: Command /opt/nec/ve/bin/vecmd not found on system, '
                   'skipping hook execution.' % caller_name())
        e.accept()
    # If an exception occurs, jobutil must be set to something
    jobutil = None
    vnode = None
    hostname = pbs.get_local_nodename()
    pbs.logmsg(pbs.EVENT_DEBUG4, '%s: Host is %s' % (caller_name(), hostname))
    # Log the hook event type
    pbs.logmsg(pbs.EVENT_DEBUG4, '%s: Hook name is %s' %
               (caller_name(), e.hook_name))
    try:
        set_global_vars()
    except Exception:
        pbs.logmsg(pbs.EVENT_DEBUG,
                   str(traceback.format_exc().strip().splitlines()))
        e.reject('%s: Hook failed to initialize configuration properly' %
                     caller_name())
    # Instantiate the hook utility class
    try:
        hookutils = HookUtils()
        pbs.logmsg(pbs.EVENT_DEBUG4, '%s: Hook utility class instantiated' %
                   caller_name())
    except Exception:
        pbs.logmsg(pbs.EVENT_DEBUG,
                   str(traceback.format_exc().strip().splitlines()))
        e.reject('%s: Failed to instantiate hook utility class' %
                   caller_name())

    # Instantiate the SX Aurora utility class
    try:
        sxautils = SXAUtils(hostname)
        pbs.logmsg(pbs.EVENT_DEBUG4, '%s: SX Aurora utility class '
                                     'instantiated' % caller_name())
    except Exception:
        pbs.logmsg(pbs.EVENT_DEBUG,
                   str(traceback.format_exc().strip().splitlines()))
        e.reject('%s: Failed to instantiate hook utility class' %
                   caller_name())

    # Bail out if there is no handler for this event
    if not hookutils.hashandler(e.type):
        pbs.logmsg(pbs.EVENT_DEBUG, '%s: %s event not handled by this hook' %
                   (caller_name(), hookutils.event_name(e.type)))
        e.accept()
    try:
        # Instantiate the job utility class first so jobutil can be accessed
        # by the exception handlers.
        if hasattr(e, 'job') and not e.type == pbs.QUEUEJOB:
            jobutil = JobUtils(e.job)
            pbs.logmsg(pbs.EVENT_DEBUG4,
                       '%s: Job information class instantiated' %
                       caller_name())
        else:
            pbs.logmsg(pbs.EVENT_DEBUG4, '%s: Event does not include a job' %
                       caller_name())
       
        sxa_lock_file = os.path.join(PBS_MOM_HOME, 'mom_priv', 'sxa.lock')

        if hasattr(e, 'vnode_list'):
            if hostname in e.vnode_list:
                vnode = e.vnode_list[hostname]
        with Lock(sxa_lock_file):
            # Only write this once we grabbed the lock,
            # otherwise *another* event could actually win the lock
            # even though *this* event printed this message last,
            # and we'd be confused about the event that the winner services
            loglevel = pbs.EVENT_DEBUG2
            if hasattr(e, 'job') and hasattr(e.job, 'id'):
                pbs.logmsg(loglevel, '%s: Event type is %s, job ID is %s'
                           % (caller_name(), hookutils.event_name(e.type),
                              e.job.id))
            else:
                pbs.logmsg(loglevel, '%s: Event type is %s'
                           % (caller_name(), hookutils.event_name(e.type)))

            # Call the appropriate handler
            if hookutils.invoke_handler(sxautils, jobutil):
                pbs.logmsg(pbs.EVENT_DEBUG4,
                           '%s: Hook handler returned success for %s event' %
                           (caller_name(), hookutils.event_name(e.type)))
                e.accept()
            else:
                pbs.logmsg(pbs.EVENT_DEBUG,
                           '%s: Hook handler returned failure for %s event' %
                           (caller_name(), hookutils.event_name(e.type)))
                e.reject()
    except SystemExit:
        # The event.accept() and event.reject() methods generate a SystemExit
        # exception.
        pass
    except UserError as exc:
        # User must correct problem and resubmit job, job gets deleted
        msg = ('User error in %s handling %s event' %
               (e.hook_name, hookutils.event_name(e.type)))
        if jobutil != None:
            msg += ' for job %s' % (e.job.id)
            try:
                e.job.delete()
                msg += ' (deleted)'
            except Exception:
                msg += ' (deletion failed)'
        msg += (': %s %s' % (exc.__class__.__name__, str(exc.args)))
        e.reject(msg)
    except ProcessingError as exc:
        pbs.logmsg(pbs.EVENT_DEBUG,
                   str(traceback.format_exc().strip().splitlines()))
        msg = ('Processing error in %s handling %s event' %
               (e.hook_name, hookutils.event_name(e.type)))
        if jobutil != None:
            msg += ' for job %s' % (e.job.id)
        msg += (': %s %s' % (exc.__class__.__name__, str(exc.args)))
        e.reject(msg)
    except Exception as exc:
        # Catch all other exceptions and report them, job gets held
        # and a stack trace is logged
        pbs.logmsg(pbs.EVENT_DEBUG,
                   str(traceback.format_exc().strip().splitlines()))
        msg = ('Unexpected error in %s handling %s event' %
               (e.hook_name, hookutils.event_name(e.type)))
        if jobutil != None:
            msg += ' for job %s' % (e.job.id)
            try:
                e.job.Hold_Types = pbs.hold_types('s')
                e.job.rerun()
                msg += ' (system hold set)'
            except Exception:
                msg += ' (system hold failed)'
        msg += (': %s %s' % (exc.__class__.__name__, str(exc.args)))
        e.reject(msg)


if (__name__ == 'builtins') or (__name__ == '__builtin__'):
    START = time.time()
    try:
        main()
    except SystemExit:
        # The event.accept() and event.reject() methods generate a
        # SystemExit exception.
        pass
    except Exception:
        # "Should never happen" since main() is supposed to catch these
        pbs.logmsg(pbs.EVENT_DEBUG,
                   str(traceback.format_exc().strip().splitlines()))
        e.reject(str(traceback.format_exc().strip().splitlines()))
    finally:
        loglevel = pbs.EVENT_DEBUG2
        if hasattr(e, 'job') and hasattr(e.job, 'id'):
            pbs.logmsg(loglevel, 'Hook ended: %s, job ID %s, '
                       'event_type %s (elapsed time: %0.4lf)' %
                       (e.hook_name,
                        e.job.id,
                        str(e.type),
                        (time.time() - START)))
        else:
            pbs.logmsg(loglevel, 'Hook ended: %s, '
                       'event_type %s (elapsed time: %0.4lf)' %
                       (e.hook_name,
                        str(e.type),
                        (time.time() - START)))
