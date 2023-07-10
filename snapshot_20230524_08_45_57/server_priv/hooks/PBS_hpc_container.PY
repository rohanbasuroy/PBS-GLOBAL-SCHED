#!/usr/bin/env python3

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

# NOTES:
#
# This hook handles all of the operations necessary for PBS to support
# the running jobs/applications inside containers, launching
# a separate container to run each job with the exact same environment
# that it provides to any other non-container job.
#
# This hook services the following events:
# - exechost_periodic
# - execjob_launch
# - execjob_end
# - queuejob

import copy
import glob
import grp
import json as js
import os
import pwd
import re
import socket
import stat
import string
import subprocess
import sys
import traceback
import fcntl

import pbs

e = pbs.event()


# Errors in configuring docker.
class ConfigError(Exception):
    pass

# ============================================================================
# Utility functions
# ============================================================================


def caller_name():
    """
    Returns the name of the calling function or method.
    """
    return str(sys._getframe(1).f_code.co_name)


def decode_dict(data):
    """
    Method to convert dictionary from unicode to utf-8
    """
    returnvalue = {}
    for key, value in data.items():
        if isinstance(key, (bytes, bytearray)):
            key = str(key, 'utf-8')
        if isinstance(value, (bytes, bytearray)):
            value = str(value, 'utf-8')
        elif isinstance(value, list):
            value = decode_list(value)
        elif isinstance(value, dict):
            value = decode_dict(value)
        returnvalue[key] = value
    return returnvalue


def parse_config_file():
    """
    Read in the config file
    """
    pbs.logmsg(pbs.EVENT_DEBUG4, "%s: Method called " %
               (caller_name()))
    cfg_file = pbs.hook_config_filename
    pbs.logmsg(pbs.EVENT_DEBUG4, "%s: Config file is %s" %
               (caller_name(), cfg_file))
    try:
        cfg = js.load(open(cfg_file, 'r'), object_hook=decode_dict)
    except IOError:
        raise ConfigError("I/O error in reading config file")
    except ValueError:
        raise ConfigError("JSON parsing error in reading config file")
    except Exception:
        raise
    return cfg


def decode_list(data):
    """
    Method to convert lists from unicode to utf-8
    """
    retval = []
    for item in data:
        if isinstance(item, (bytes, bytearray)):
            item = str(item, 'utf-8')
        elif isinstance(item, list):
            item = decode_list(item)
        elif isinstance(item, dict):
            item = decode_dict(item)
        retval.append(item)
    return retval


def exec_cmd(cmd):
    """
    Run given command and return output
    :param cmd: command to run
    :type cmd: list
    """
    pbs.logmsg(pbs.EVENT_DEBUG4, "%s: Method called" % (caller_name()))
    pbs.logmsg(pbs.EVENT_DEBUG4, "Cmd is: %s" % " ".join(cmd))
    try:
        process = subprocess.Popen(cmd, shell=False,
                                   stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE)
        output, err = process.communicate()
    except OSError:
        pbs.logmsg(pbs.EVENT_DEBUG, "Failed to execute: %s" %
                   ' '.join(cmd))
        return None
    except ValueError:
        pbs.logmsg(pbs.EVENT_DEBUG, "Failed to execute: %s" %
                   ' '.join(cmd))
        pbs.logmsg(pbs.EVENT_DEBUG, "Invalid arguments passed.")
        return None
    status = process.returncode
    if status != 0:
        pbs.logmsg(pbs.EVENT_DEBUG,
                   "Unable to run command: %s.\n err: %s" %
                   (' '.join(cmd), err))
        return None
    return output.decode('utf-8')


def validate_config(conf):
    """
    Validate config parameters in queuejob hook event
    :param conf: Hook config data
    :type conf: dict
    """
    pbs.logmsg(pbs.EVENT_DEBUG4, "%s: Method called" % (caller_name()))
    if 'container_resource_default_value' not in conf.keys() or \
            not conf['container_resource_default_value']:
        msg = "container_resource_default_value not set in "
        "config"
        pbs.logmsg(pbs.EVENT_DEBUG, "%s" % msg)
        msg = "config parameter container_resource_default_value "
        "is empty/None"
        e.reject(msg)
    if not conf["allowed_registries"]:
        msg = "Config parameter allowed_registries is empty."
        pbs.logmsg(pbs.EVENT_DEBUG, "%s" % msg)
        msg = "Requested container image cannot be validated with " \
            "empty allowed_registries config parameter"
        e.reject(msg)
    return


def get_auth_param(config):
    """
    Method to read and parse the container registry
    authentication parameters from
    <cred_base_path>/job_owner/.container/tokens.json file
    :param config: Hook config data
    :type config: dict
    """
    pbs.logmsg(pbs.EVENT_DEBUG4, "%s: Method called" % (caller_name()))
    contents = {}
    if 'cred_base_path' not in config.keys() or \
            not config['cred_base_path']:
        pbs.logmsg(pbs.EVENT_DEBUG,
                   "cred_base_path is not set in the config")
        pbs.logmsg(pbs.EVENT_DEBUG,
                   "Looking for .container/tokens.json with base path /home")
        base_path = os.path.join(os.sep, "home")
    else:
        base_path = config["cred_base_path"]
    file_path = os.path.join(base_path, e.job.euser, ".container",
                             "tokens.json")
    try:
        contents = js.load(open(file_path, 'r'), object_hook=decode_dict)
    except IOError:
        pbs.logmsg(pbs.EVENT_DEBUG, "I/O error in reading auth file")
        return contents
    except ValueError:
        pbs.logmsg(pbs.EVENT_DEBUG, "JSON parsing error in reading auth file")
        return contents
    return contents


#
# CLASS HookUtils
#
class HookUtils:

    def __init__(self):
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
            'handler': None
        }
        self.hook_events[pbs.EXECJOB_PROLOGUE] = {
            'name': 'execjob_prologue',
            'handler': None
        }
        self.hook_events[pbs.EXECJOB_EPILOGUE] = {
            'name': 'execjob_epilogue',
            'handler': None
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
            'handler': None
        }
        self.hook_events[pbs.EXECJOB_ATTACH] = {
            'name': 'execjob_attach',
            'handler': None
        }

    def event_name(self, hooktype):
        """
        Method returns name of the event triggering the hook.
        :param hooktype: Event type triggering the hook.
        :type hooktype: string
        """
        pbs.logmsg(pbs.EVENT_DEBUG4, "%s: Method called" % (caller_name()))
        if hooktype in self.hook_events:
            return self.hook_events[hooktype]['name']
        else:
            pbs.logmsg(pbs.EVENT_DEBUG,
                       "%s:Hook Type: %s not found" %
                       (caller_name(), hooktype))
            return None

    def hashandler(self, hooktype):
        """
        Method to check if the hook file has a event handler
        for the event type being trigured in the hook.
        """
        pbs.logmsg(pbs.EVENT_DEBUG4, "%s: Method called" % (caller_name()))
        if hooktype in self.hook_events:
            return self.hook_events[hooktype]['handler'] != None
        else:
            return False

    def invoke_handler(self, hooktype, conf, cgroup_enabled):
        """
        Method to invoke the event handler supported by the hook
        """
        pbs.logmsg(pbs.EVENT_DEBUG4, "%s: Method called" % (caller_name()))
        if self.hashandler(hooktype):
            result = self.hook_events[hooktype][
                'handler'](conf, cgroup_enabled)
            return result
        else:
            pbs.logmsg(pbs.EVENT_DEBUG,
                       "%s: %s event not handled by this hook." %
                       (caller_name(), self.event_name(e.type)))

    def _execjob_launch_handler(self, conf, cgroup_enabled):
        """
        Method invoked for execjob_launch hook event.
        :param conf: data from configuration file.
        :type conf: Dictionary
        """
        pbs.logmsg(pbs.EVENT_DEBUG4, "%s: Method called" % (caller_name()))
        cont_type = None
        cont_res = conf['container_resource_name']
        chunk = str(e.job.schedselect).split("+")[0].split(":")
        for res in chunk:
            if res.split("=")[0].lower() == cont_res.lower():
                cont_type = res.split("=")[1]
                break
        try:
            if conf[cont_type]:
                for key in conf[cont_type]:
                    conf[key] = conf[cont_type][key]
        except KeyError:
            e.reject("Config parameters for %s not found" % cont_type)
        # Instantiate the identified container class for creating a container
        # and launching a job in it.
        if cont_type == "singularity":
            hpsg = Hpc_Singularity(conf)
            hpsg.launch_job()
            return True
        hpcd = Hpc_Docker(conf)
        args = copy.deepcopy(e.argv)
        cgps = Cgroups(conf, cgroup_enabled)
        hpcd.create_container(cgroup_enabled)
        hpcd.launch_job(args, cgroup_enabled)
        return True

    def _execjob_end_handler(self, conf, cgroup_enabled):
        """
        Method invoked for execjob_end hook event.
        """
        pbs.logmsg(pbs.EVENT_DEBUG4, "%s: Method called" % (caller_name()))
        cont_res = conf['container_resource_name']
        cont_type = None
        chunk = str(e.job.schedselect).split("+")[0].split(":")
        for res in chunk:
            if res.split("=")[0].lower() == cont_res.lower():
                cont_type = res.split("=")[1]
                break
        if cont_type == "singularity":
            return True
        try:
            if conf[cont_type]:
                for key in conf[cont_type]:
                    conf[key] = conf[cont_type][key]
        except KeyError:
            e.reject("Config parameters for %s not found" % cont_type)
        # Instantiate the Cgroups class for updating job usage
        # before the container is cleaned up.
        cgps = Cgroups(conf, cgroup_enabled)
        if not cgroup_enabled:
            cgps.update_job_usage(e.job)
        # Instantiate the Hpc_Docker class for cleaning up the container
        hpcd = Hpc_Docker(conf)
        hpcd.shutdown_job()
        return True

    def _exechost_periodic_handler(self, conf, cgroup_enabled):
        """
        Method invoked for exechost_periodic hook event.
        """
        pbs.logmsg(pbs.EVENT_DEBUG4, "%s: Method called" % (caller_name()))
        cont_res = conf['container_resource_name']
        cont_type = None
        for k in e.job_list.keys():
            job = e.job_list[k]
            # for a job with multi-chunk request,
            # like 1:ncpus=1:mem=10mb+1:ncpus=1,
            # split with '+' to get all resource requests in a chunk.
            # In this chunk go through each resource request,
            # by spliting with ':'
            chunk = str(job.schedselect).split("+")[0].split(":")
            for res in chunk:
                # For a resource request like container_engine=docker,
                # check resource key (split("=")[0]) matches
                # with conf['container_resource_name']
                # and then store the value (split("=")[1])
                # of this request in cont_type
                if res.split("=")[0].lower() == cont_res.lower():
                    cont_type = res.split("=")[1]
                    break
            if cont_type == None:
                # not a container job
                continue
            elif cont_type == "docker":
                try:
                    if conf[cont_type]:
                        for key in conf[cont_type]:
                            conf[key] = conf[cont_type][key]
                except KeyError:
                    e.reject("Config parameters for %s not found" % cont_type)
                # Instantiate the ContainerUtils class
                contutils = ContainerUtils(conf)
                # Cleanup containers for docker jobs not present on the node
                contutils.cleanup_orphans(list(e.job_list.keys()))
                # Instantiate the Cgroups class for updating
                # docker job(s) usage data.
                cgps = Cgroups(conf, cgroup_enabled)
                if not cgroup_enabled:
                    cgps.update_job_usage(job)
        return True

    def _queuejob_handler(self, conf, cgroup_enabled):
        """
        Method invoked for queuejob hook event.
        """
        pbs.logmsg(pbs.EVENT_DEBUG4, "%s: Method called" % (caller_name()))
        # validate Config parameters:
        validate_config(conf)
        # Append a custom resource (value set in config) to
        # select spec as apart of resource request for jobs to
        # be placed on nodes in which the container daemon is active.
        cus_req_flag = 0
        res_req = str(conf['container_resource_name']) + \
            "=" + str(conf['container_resource_default_value'])
        str2 = ""
        str1 = str(e.job.Resource_List.select)
        if str1 == "None":
            str1 = '1:' + res_req
        else:
            chunk = str1.split("+")
            for res_list in chunk:
                chk_res = res_list.split(":")
                for res in chk_res:
                    if res.split("=")[0] == \
                            conf['container_resource_name']:
                        cus_req_flag = 1
                if cus_req_flag == 0:
                    res_list = res_list + ":" + res_req
                # reset flag
                cus_req_flag = 0
                str2 += res_list + "+"
            str1 = str2[:-1]
        e.job.Resource_List.select = pbs.select(str1)
        pbs.logmsg(pbs.EVENT_DEBUG4, str1)
        # check for mixed chunk requests of different
        # container engines
        cont_type = None
        chunks = str(e.job.Resource_List.select).split("+")
        for chunk in chunks:
            chk_res = chunk.split(":")
            for res in chk_res:
                if res.split("=")[0].lower() == \
                  conf['container_resource_name'].lower():
                    if not cont_type:
                        cont_type = res.split("=")[1]
                    elif cont_type != res.split("=")[1]:
                        pbs.logmsg(pbs.EVENT_DEBUG,
                                   "chunk level request mismatch "
                                   "for job:%s, %s != %s" %
                                   (e.job.id, cont_type,
                                    res.split("=")[1]))
                        e.reject("mixed chunk requests of different"
                                 "container technologies not supported")
        if cont_type == "docker" and \
            "nvidia_docker_cmd" not in conf['docker'] and  \
            "ngpus" in str(e.job.Resource_List.select) and \
            cgroup_enabled == 0:
            e.reject("Enable pbs_cgroups hook to run GPU containers using docker")
        try:
            if conf[cont_type]:
                for key in conf[cont_type]:
                    conf[key] = conf[cont_type][key]
            pbs.logmsg(pbs.EVENT_DEBUG, "Updated Config: %s" % conf)
        except KeyError:
            e.reject("Config parameters for %s not found" % cont_type)
        if 'container_cmd' not in conf.keys() or not conf['container_cmd']:
            pbs.logmsg(pbs.EVENT_DEBUG,
                       "container_cmd is required to launch "
                       "job inside %s container" %
                       cont_type)
            e.reject("config parameter container_cmd is empty/None")

        if cont_type == "singularity":
            img_source = "container_image_source"
            if img_source not in conf.keys() or not conf[img_source]:
                pbs.logmsg(pbs.EVENT_DEBUG, "singularity image source is "
                           "required for running containers")
                msg = "config parameter container_image_source is empty/None"
                e.reject(msg)

        # validate container image requested with allowed_registries
        cont_image = None
        try:
            if e.job.Resource_List['container_image']:
                cont_image = e.job.Resource_List['container_image']
        except AttributeError:
            pass
        if cont_image == None:
            image = "CONTAINER_IMAGE="
            varlist = str(e.job.Variable_List)
            vlist = re.split(r'(?<!\\),', varlist)
            for i in vlist:
                if i.startswith(image):
                    name = i.split("=")
                    cont_image = name[1]
        pbs.logmsg(pbs.EVENT_DEBUG,
                   "%s: container image %s requested" %
                   (e.job.id, cont_image))
        image_list = cont_image.split('/')
        # if the url exists in container_image string,
        # then it has to be in the first element
        if image_list[0] not in conf["allowed_registries"]:
            # check if the image requested has complete path
            if len(image_list) > 2:
                if "PBS_ALL" in conf["allowed_registries"]:
                    msg = "PBS_ALL included in allowed_registries to " + \
                        "whitelist all registries"
                    pbs.logmsg(pbs.EVENT_DEBUG, "%s" % msg)
                    return True
                if (cont_type == "singularity" and
                  conf["container_image_source"] == "docker") or \
                  (cont_type == "docker"):
                    # reject the job here because the repo name
                    # in image_list[0] has the repo value which is not
                    # present in allowed_registries
                    pbs.logmsg(pbs.EVENT_DEBUG,
                               "%s not in allowed_registries list of config" %
                               image_list[0])
                    msg = "container registry requested in job not in " + \
                        "allowed_registries list"
                    e.reject(msg)
            else:
                # container image does not have a registry and PBS assumes
                # here that user wants image pulled from default registry.
                msg = "repo value %s could not be validated." % image_list[0]
                msg += " Using default repo %s" % conf["allowed_registries"][0]
                pbs.logmsg(pbs.EVENT_DEBUG, "%s" % msg)
        return True


#
# CLASS ContainerUtils
#
class ContainerUtils:

    def __init__(self, conf):
        self.cfg = conf
        self.container_cmd = self.cfg["container_cmd"]
        self.container_exe = os.path.split(self.container_cmd)[-1]
        self.hostname = pbs.get_local_nodename()
        self.exec_dir = pbs.pbs_conf['PBS_EXEC']
        if pbs.pbs_conf['PBS_MOM_HOME']:
            self.home_dir = pbs.pbs_conf['PBS_MOM_HOME']
        else:
            self.home_dir = pbs.pbs_conf['PBS_HOME']

    def cleanup_orphans(self, local_jobs):
        """
        Method to clean up left behind/orphaned containers
        for jobs not present on the node.
        :param local_jobs: list of jobs on the local node.
        :type local_jobs: List
        """
        pbs.logmsg(pbs.EVENT_DEBUG4, "%s: Method called" % (caller_name()))
        # clean up containers for jobs not running on the node
        cmd = [self.container_exe, "ps", "-a", "--format", "\"{{.Names}}\""]
        output = exec_cmd(cmd)
        if output == None:
            return
        containers_list = {}
        jobid_re = re.compile(
            r"[0-9]+(_[0-9]+_)?\.(" + pbs.pbs_conf['PBS_SERVER'] + ")$")
        containers_list = output.split()
        for container in containers_list:
            pbs.logmsg(pbs.EVENT_DEBUG4, "Container name: %s" %
                       container[1:-1])
            jobid = jobid_re.search(container[1:-1])
            if jobid != None:
                # For job part of a job array
                jobno = container[1:-1].split('.')[0]
                jobid = jobno.replace("_", "[", 1).replace("_", "]", 1) + \
                    '.' + pbs.pbs_conf['PBS_SERVER']
                if jobid not in local_jobs:
                    # if job is in suspended state, container stays.
                    if not self.job_to_be_ignored(jobid):
                        pbs.logmsg(
                            pbs.EVENT_DEBUG,
                            "Cleaning up orphaned container: %s" %
                            container)
                        cmd = [self.container_exe, "stop", container[1:-1]]
                        output = exec_cmd(cmd)
                        if output == None:
                            msg = "Failed to run the command %s" % \
                                " ".join(cmd)
                            pbs.logmsg(pbs.EVENT_DEBUG, "%s" % msg)
                            return
                        pbs.logmsg(pbs.EVENT_DEBUG,
                                   "%s cleaned up successfully" %
                                   container[1:-1])
        return

    def job_to_be_ignored(self, jobid):
        """
        Function to leave out suspended jobs while
        cleaning up orphaned containers on the node.
        :param jobid: Id of the job
        :type jobid: string
        :returns: Boolean
        """
        pbs.logmsg(pbs.EVENT_DEBUG4, "%s: Method called" % (caller_name()))
        printjob_cmd = os.path.join(self.exec_dir, 'bin', 'printjob')
        jobfile = jobid + ".JB"
        jobdir = os.path.join(self.home_dir, 'mom_priv', 'jobs', jobfile)
        cmd = [printjob_cmd, '-a', jobdir]
        substate = None
        out = exec_cmd(cmd)
        if out == None:
            return False
        substate_re = re.compile(r"substate:\s+(?P<jobstate>\S+)\s+")
        substate = substate_re.search(out)
        if substate != None:
            substate = substate.group().split(':')[1].strip()
            pbs.logmsg(pbs.EVENT_DEBUG4,
                       "Job %s has substate %s" % (jobid, substate))
            suspended_substates = ['0x2b', '0x2d', 'unknown']
            return substate in suspended_substates
        else:
            return False


#
# CLASS Cgroups
#
class Cgroups:

    def __init__(self, conf, cgroup_enabled):
        self.cgroup_parent_dir = "pbs_jobs.service/jobid/"
        if not cgroup_enabled:
            self.cgroups_paths = self.get_paths('docker')
        else:
            self.cgroups_paths = self.get_paths(self.cgroup_parent_dir)
        if not self.cgroups_paths:
            pbs.logmsg(pbs.EVENT_DEBUG, "%s: No cgroups mounted" %
                       (caller_name()))
        self.cfg = conf
        self.image = "CONTAINER_IMAGE="
        self.hostname = pbs.get_local_nodename()
        self.exec_dir = pbs.pbs_conf['PBS_EXEC']

    def update_job_usage(self, curr_job):
        """
        Method to Update resource utilization of docker jobs
        :param curr_job: A job from the list of jobs on the node
        :type curr_job: string
        """
        pbs.logmsg(pbs.EVENT_DEBUG4, "%s: Method called" % (caller_name()))
        try:
            container_job = 0
            try:
                if curr_job.Resource_List['container_image']:
                    container_job = 1
            except AttributeError:
                # for backward compatibility
                varlist = str(curr_job.Variable_List)
                vlist = re.split(r'(?<!\\),', varlist)
                for i in vlist:
                    if i.startswith(self.image):
                        container_job = 1
            if container_job == 0:
                # No need to run any container instance....just bail out!
                return
            pbs.logmsg(pbs.EVENT_DEBUG4, "JOB NAME : %s " % curr_job.id)
            container_name = "name=" + curr_job.id
            docker_cmd = self.cfg["docker"]["container_cmd"]
            docker_exe = os.path.split(docker_cmd)[-1]
            cmd = [docker_exe, "ps", "--filter",
                   container_name, "--quiet", "--no-trunc"]
            output = exec_cmd(cmd)
            if output == None:
                return
            if output == "":
                # Container not running or not up yet
                return

            container_id = str(output[:-1])
            # cpu_percent
            stats_format = (
                '"{"cpu_per":{{.CPUPerc}},"MemUsage":{{.MemUsage}}}"')
            cmd = [docker_exe, "stats", "--no-stream",
                   container_id, "--format", stats_format]
            try:
                process = subprocess.Popen(cmd, shell=False,
                                           stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE)
                output, err = process.communicate()
            except ValueError:
                pbs.logmsg(pbs.EVENT_DEBUG, "Failed to execute: %s" %
                           ' '.join(cmd))
                pbs.logmsg(pbs.EVENT_DEBUG, "Invalid arguments passed.")
                return
            status = process.returncode
            if status != 0:
                if e.type == pbs.EXECHOST_PERIODIC:
                    if curr_job in e.job_list.keys():
                        pbs.logmsg(pbs.EVENT_DEBUG,
                                   "Unable to run command: %s.\n err: %s" %
                                   (cmd, err))
                else:
                    pbs.logmsg(pbs.EVENT_DEBUG,
                               "Container already cleaned up.")
                return
            stats = str(output).strip()
            cpu_per = ((stats.split(":")[1]).split(","))[0]
            # cput
            cpu_usage_path = self.assemble_path(
                'cpuacct', self.cgroups_paths['cpuacct'], container_id)
            cpuacct_usage = self.get_cgroup_usage(cpu_usage_path)
            # converting cput value from nanoseconds to seconds
            # because PBS deals in kb
            cpu_time = int(cpuacct_usage / 1000000000)
            minute, sec = divmod(cpu_time, 60)
            hrs, minute = divmod(minute, 60)
            docker_cput = str("%02d:%02d:%02d" % (hrs, minute, sec))
            # mem_usage
            memory_usage_path = self.assemble_path(
                'memory', self.cgroups_paths['memory'], container_id)
            memory_usage = self.get_cgroup_usage(memory_usage_path)
            # memory_usage is in bytes. It is needed to be converted
            # into kb because PBS deals in kb
            memory_in_kilobytes = int(memory_usage * 0.001)
            memory_for_pbs = str(memory_in_kilobytes) + "kb"
            # vmem_usage
            vmem_usage_path = self.assemble_path(
                'memsw', self.cgroups_paths['memory'], container_id)
            vmem_usage = self.get_cgroup_usage(vmem_usage_path)
            # converting vmem_usage value from bytes to kilobytes
            # because PBS deals in kb.
            vmem_in_kilobytes = int(vmem_usage * 0.001)
            vmem_for_pbs = str(vmem_in_kilobytes) + "kb"
            # logs
            mem = curr_job.resources_used["mem"]
            curr_job.resources_used["mem"] = pbs.size(str(memory_for_pbs))
            cput = curr_job.resources_used["cput"]
            curr_job.resources_used["cput"] = pbs.duration(docker_cput)
            vmem = curr_job.resources_used["vmem"]
            curr_job.resources_used["vmem"] = pbs.size(str(vmem_for_pbs))
            cpupercent = curr_job.resources_used["cpupercent"]
            curr_job.resources_used["cpupercent"] = int(
                (cpu_per.split(".")[0]))
            pbs.logmsg(
                pbs.EVENT_DEBUG4,
                "Resources used cpupercent, mem, vmem and cput: \
                %s, %s, %s and %s" %
                (cpupercent, mem, vmem, cput))

        except (ValueError, TypeError):
            pass
        except:
            pbs.logmsg(pbs.EVENT_DEBUG, str(
                traceback.format_exc().strip().splitlines()))
        return

    def get_paths(self, parent):
        """
        Method to create a dictionary of the cgroup subsystems for reading
        resource utilization of a job.
        :returns: Dictionary
        """
        pbs.logmsg(pbs.EVENT_DEBUG4, "%s: Method called" % (caller_name()))
        paths = {}
        # Loop through the mounts and collect the ones for cgroups
        try:
            with open(os.path.join(os.sep, "proc", "mounts"), 'r') as fd:
                for line in fd:
                    entries = line.split()
                    if entries[2] != "cgroup":
                        continue
                    flags = entries[3].split(',')
                    if 'cpuacct' in flags:
                        paths['cpuacct'] = \
                            os.path.join(entries[1], parent)
                    if 'memory' in flags:
                        paths['memory'] = \
                            os.path.join(entries[1], parent)
                    if 'freezer' in flags:
                        paths['freezer'] = \
                            os.path.join(entries[1], parent)
                    if 'blkio' in flags:
                        paths['blkio'] = \
                            os.path.join(entries[1], parent)
                    if 'name=systemd' in flags:
                        paths['systemd'] = \
                            os.path.join(entries[1], parent)
                    if 'net_cls' in flags:
                        paths['net_cls'] = \
                            os.path.join(entries[1], parent)
                    if 'perf_event' in flags:
                        paths['perf_event'] = \
                            os.path.join(entries[1], parent)
        except IOError:
            pbs.logmsg(pbs.EVENT_DEBUG,
                       "I/O error: Cgroup paths not detected.")
        if not paths:
            pbs.logmsg(pbs.EVENT_DEBUG,
                       str(traceback.format_exc().strip().splitlines()))
        return paths

    def get_cgroup_usage(self, path):
        """
        Method to read resource utilization from a Cgroup subsystem.
        :param path: path to the Cgroup subsytem
        :type path: String
        :returns: int
        """
        pbs.logmsg(pbs.EVENT_DEBUG4, "%s: Method called" % (caller_name()))
        try:
            with open(path, 'r') as fd:
                return int(fd.readline().strip())
        except IOError:
            pbs.logmsg(pbs.EVENT_DEBUG,
                       "I/O error in reading file: %s" % path)
            return None

    def assemble_path(self, subsys, moupnt, container_id):
        """
        Determine the path for a cgroup directory given the subsystem
        :param subsys: path to the subsystem
        :type subsys: String
        :param moupnt: mount path of the subgroup
        :type moupnt: Dictionary
        :param container_id: container ID the container
        :type container_id: String
         corresponding to the job
        :returns: String
        """
        pbs.logmsg(pbs.EVENT_DEBUG4, "%s: Method called" % (caller_name()))
        if subsys == 'cpuacct':
            file_name = 'cpuacct.usage'
        elif subsys == 'memsw':
            file_name = 'memory.memsw.usage_in_bytes'
        else:
            file_name = 'memory.usage_in_bytes'
        return os.path.join(moupnt, container_id, file_name)


class Hpc_Singularity:
    """
    This class is to handle the setup of
    Singularity containers for hpc jobs.
    """

    def __init__(self, conf):
        if pbs.pbs_conf['PBS_MOM_HOME']:
            self.home_dir = pbs.pbs_conf['PBS_MOM_HOME']
        else:
            self.home_dir = pbs.pbs_conf['PBS_HOME']
        self.exec_dir = pbs.pbs_conf['PBS_EXEC']
        self.jobdir = e.env['PBS_JOBDIR']
        self.user_home = e.env['PBS_O_HOME']
        self.jid = e.job.id
        self.euser = e.job.euser
        self.singularity_cmd = conf["container_cmd"]
        self.cfg = conf
        self.cont_image = None

    def cache_container_image(self, image_source, environ):
        """
        Method to do a login and exec of the image
        and return the image path
        :param image_source: singularity image source from conf
        :type image_source: string
        :param envrion: environment variable list
        :type environ: list
        """
        pbs.logmsg(pbs.EVENT_DEBUG4, "%s: Method called " % (caller_name()))
        user_image = image_source + self.cont_image
        # Read cache path from  config if set,
        # else set it in user home which is the
        # default singularity cache path
        path = self.cfg['container_cache_path']
        if not path:
            path = os.path.join(self.user_home,
                                ".singularity", "cache")
        environ['SINGULARITY_CACHEDIR'] = path
        # Run singularity exec command with login details.
        # This command will do a login and then pull the image
        # and save the container file in container_cache_path directory.
        os.chdir(self.user_home)
        cmd = ["sudo", "-E", "-H", "-u", self.euser]
        exec_args = self.add_args()
        cmd += [self.singularity_cmd, 'exec']
        for arg in exec_args:
            cmd += [arg]
        cmd += [user_image]
        cmd += ["hostname"]
        msg = "command %s failed" % ' '.join(cmd)
        try:
            process = subprocess.Popen(cmd, shell=False, env=environ,
                                       stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE)
            output, err = process.communicate()
        except OSError:
            pbs.logmsg(pbs.EVENT_DEBUG, "Failed to execute: %s" %
                       ' '.join(cmd))
            e.reject(msg)
        except ValueError:
            pbs.logmsg(pbs.EVENT_DEBUG, "Failed to execute: %s" %
                       ' '.join(cmd))
            pbs.logmsg(pbs.EVENT_DEBUG, "Invalid arguments passed.")
            e.reject(msg)
        status = process.returncode
        if status != 0:
            pbs.logmsg(pbs.EVENT_DEBUG,
                       "Unable to run command: %s.\n err: %s" %
                       (' '.join(cmd), err))
            if "invalid username/password":
                e.reject("Container login Failed")
            e.reject(msg)

        # the saved container file in cache will be of the format
        # imagename_tag.sif
        ret = self.cont_image.find(":")
        if ret == -1:
            name = self.cont_image.split('/')[-1] + "_latest.sif"
        else:
            name = self.cont_image.split('/')[-1].replace(":", "_") + ".sif"
        user_image = ""
        for root, dirs, files in os.walk(path):
            if name in files:
                user_image = os.path.join(root, name)
        if not user_image:
            msg = "Singularity image file %s not found in path %s" % \
                (name, path)
            e.reject(msg)
        pbs.logmsg(pbs.EVENT_DEBUG,
                   "Container image %s pulled sucessfully" %
                   self.cont_image)
        return user_image

    def check_reg(self, auth_data, docker=False):
        """
        Method to check if reg is white-listed in
        allowed_registries and build auth_data with
        default registry value.
        :param auth_data: user authentication data
        :type auth_data: dict
        :param docker: indicates if image source is docker
        :type docker: boolean
        """
        pbs.logmsg(pbs.EVENT_DEBUG4, "%s: Method called " % (caller_name()))

        # extract registry url from container_image
        reg = ""
        default_reg = self.cfg["allowed_registries"][0]
        image_list = self.cont_image.split('/')
        # if registry url exists in container_image string,
        # then it has to be the first element
        # check if image_list[0] is present in allowed_registries
        if image_list[0] in self.cfg["allowed_registries"]:
            reg = image_list[0]
        else:
            if len(image_list) > 2 and docker:
                # allowed_registries must have PBS_ALL
                reg = image_list[0]
            else:
                reg = default_reg
                msg = "repo value %s could not be validated." % image_list[0]
                msg += " Using default repo %s" % default_reg
                pbs.logmsg(pbs.EVENT_DEBUG, "%s" % msg)
                if docker:
                    self.cont_image = reg + '/' + self.cont_image

        if reg not in auth_data:
            auth_data[reg] = {}
            auth_data[reg]['user_id'] = self.euser
            auth_data[reg]['passwd'] = ""
        return reg, auth_data

    def singularity_security_check(self, image_source):
        """
        Method to check built in image access controls
        using job submitter's registry login data
        :param image_source: singularity image source from conf
        :type image_source: string
        """
        pbs.logmsg(pbs.EVENT_DEBUG4, "%s: Method called " % (caller_name()))
        auth_data = get_auth_param(self.cfg)
        if not auth_data:
            # no authentication
            msg = "No authentication data found."
            msg += " Skipping container authentication"
            pbs.logmsg(pbs.EVENT_DEBUG, msg)
            # bail from here because singularity exec below will do a pull
            self.cont_image = image_source + self.cont_image
            return

        if image_source == "shub://":
            # singularity hub is a public repository,
            # so there is no image authentication
            # So, we directly skip to singularity exec
            msg = "No authentication support for singularity hub"
            pbs.logmsg(pbs.EVENT_DEBUG, msg)
            self.cont_image = image_source + self.cont_image
            return
        elif image_source == "docker://":
            reg, auth_data = self.check_reg(auth_data, docker=True)
            if auth_data[reg]['passwd']:
                # use the image path
                env = os.environ
                env['SINGULARITY_DOCKER_USERNAME'] = auth_data[reg]['user_id']
                env['SINGULARITY_DOCKER_PASSWORD'] = auth_data[reg]['passwd']
                self.cont_image = self.cache_container_image(image_source, env)
                pbs.logmsg(pbs.EVENT_DEBUG,
                           "%s;%s successfully logged in to %s" %
                           (self.jid, auth_data[reg]['user_id'], reg))
                return
            else:
                msg = "No value set for passwd in .container/tokens.json file"
                pbs.logmsg(pbs.EVENT_DEBUG, msg)
                pbs.logmsg(pbs.EVENT_DEBUG, "Skipping Container login")

        else:
            # image_source == "library://"
            reg, auth_data = self.check_reg(auth_data)
            if auth_data[reg]['passwd']:
                cmd = [self.singularity_cmd, "remote", "use",
                       reg]
                out = exec_cmd(cmd)
                if out == None:
                    msg = "Failed to run the command %s" " ".join(cmd)
                    e.reject(msg)
                # singularity remote login
                os.chdir(self.user_home)
                cmd = ["sudo", "-E", "-H", "-u", self.euser]
                cmd += [self.singularity_cmd, "remote", "login"]
                # put passwd in a temp file
                temp_file = os.path.join(self.user_home, "passwd")
                try:
                    with open(temp_file, "w") as f:
                        f.write(auth_data[reg]['passwd'])
                except IOError:
                    pbs.logmsg(pbs.EVENT_DEBUG,
                               "I/O error in writing to file: %s" % path)
                    e.reject("Container login Failed")
                os.chown(temp_file, pwd.getpwnam(self.euser).pw_uid,
                         pwd.getpwnam(self.euser).pw_gid)
                cmd += ["--tokenfile", temp_file]
                cmd += [reg]
                out = exec_cmd(cmd)
                if out == None:
                    msg = "Failed to run the command %s" " ".join(cmd)
                    pbs.logmsg(pbs.EVENT_DEBUG, "%s" % msg)
                    os.remove(temp_file)
                    e.reject("Container login Failed")
                pbs.logmsg(pbs.EVENT_DEBUG, "%s successfully logged in to %s" %
                           (auth_data[reg]['user_id'], reg))
                os.remove(temp_file)
            else:
                msg = "No value set for passwd in .container/tokens.json file"
                pbs.logmsg(pbs.EVENT_DEBUG, msg)
                pbs.logmsg(pbs.EVENT_DEBUG, "Skipping Container login")
        self.cont_image = image_source + self.cont_image
        return

    def container_image(self):
        """
        Method to identify the singularity image required
        for the job to run.
        """
        pbs.logmsg(pbs.EVENT_DEBUG4, "%s: Method called " % (caller_name()))
        try:
            if e.job.Resource_List['container_image']:
                self.cont_image = e.job.Resource_List['container_image']
        except AttributeError:
            pass
        if self.cont_image == None:
            image = "CONTAINER_IMAGE="
            varlist = str(e.job.Variable_List)
            vlist = re.split(r'(?<!\\),', varlist)
            for i in vlist:
                if i.startswith(image):
                    name = i.split("=")
                    self.cont_image = name[1]
        pbs.logmsg(pbs.EVENT_DEBUG,
                   "%s: singularity container image %s requested" %
                   (e.job.id, self.cont_image))
        image_source = self.cfg["container_image_source"]
        # a list of supported URI's of the container Hub from
        # where singularity will pull and exec. Like, a
        # URI beginning with docker:// - to build from Docker Hub
        hub_list = {"library://", "docker://", "shub://"}
        for hub in hub_list:
            if image_source == hub:
                self.singularity_security_check(image_source)
                return
        self.cont_image = self.cont_image.split("/")[-1]
        ret = self.cont_image.find(":")
        if ret == -1:
            self.cont_image = self.cont_image + "_latest.sif"
        else:
            self.cont_image = self.cont_image.replace(":", "_") + ".sif"
        # path to a existing container on your local machine
        self.cont_image = os.path.join(image_source, self.cont_image)
        if not os.path.exists(self.cont_image):
            pbs.logmsg(pbs.EVENT_DEBUG,
                       "Container image source: %s does not exist" %
                       image_source)
            e.reject("Container image source path not found")
        pbs.logmsg(pbs.EVENT_DEBUG, "Container Image:%s" % self.cont_image)
        return

    def additional_binds(self):
        """
        Identify and add additional bind paths to singularity exec command
        """
        pbs.logmsg(pbs.EVENT_DEBUG4, "%s: Method called " % (caller_name()))
        files = []
        path = os.path.join(os.sep + "etc", "pbs.conf")
        files += [path]
        # add additional mount paths from config file
        for key in self.cfg["mount_paths"]:
            files += [key]
        # Steps for stagein and stageout request into the container
        if e.job.stagein:
            stagein_file = str(e.job.stagein).split(",")
            for f in stagein_file:
                destin = f.split("@")[0]
                if not os.path.isabs(destin):
                    destin = os.path.join(self.jobdir, destin)
                files += [destin]
        if e.job.stageout:
            source = str(e.job.stageout).split("@")[0]
            if os.path.isabs(source):
                if os.path.exists(source):
                    pbs.logmsg(pbs.EVENT_DEBUG4,
                               "Stageout Path Exists: %s" % source)
                else:
                    while os.path.exists(source) is False:
                        source = os.path.split(source)[0]
                    if source == "/":
                        pbs.logmsg(pbs.EVENT_DEBUG,
                                   "Stageout path does not exist!")
                        e.reject("Invalid bind mount")
                    pbs.logmsg(pbs.EVENT_DEBUG4,
                               "new Stageout path : %s" % source)
                files += [str(source)]
        return files

    def add_args(self):
        """
        Users passing arguments for singularity,
        verify with the white-list in config and
        pass them to 'singularity exec'.
        """
        pbs.logmsg(pbs.EVENT_DEBUG4, "%s: Method called " % (caller_name()))
        add_exec_args = []
        allowed = self.cfg["container_args_allowed"]
        if allowed:
            for i, arg in enumerate(allowed):
                # arguments can start with a single '-' or double '--',
                # irrespective of that, we need just the value for
                # comparing with user input in $PBS_CONTAINER_ARGS
                allowed[i] = arg.strip("-")
            try:
                if e.env['PBS_CONTAINER_ARGS']:
                    # mulitple arguments are ';' separated.
                    args = e.env['PBS_CONTAINER_ARGS'].split(";")
                    for arg in args:
                        # arguments receive values either
                        # with a equal sign or a space
                        if arg.split("=")[0].strip("-") in allowed:
                            # for values having ',' in them,
                            # PBS adds escape char '\'
                            add_exec_args += [arg.replace("\\", "")]
                        elif arg.split(" ")[0].strip("-") in allowed:
                            add_exec_args += [arg.replace("\\", "")]
                        else:
                            msg = "%s not listed in " \
                                "container_args_allowed in Config file." \
                                " Please contact Admin" % arg
                            pbs.logmsg(pbs.EVENT_DEBUG, msg)
            except KeyError:
                pass
        return add_exec_args

    def launch_job(self):
        """
        Method to launch a job inside the container
        """
        pbs.logmsg(pbs.EVENT_DEBUG4, "%s: Method called " % (caller_name()))
        try:
            if e.job.Resource_List['container_ports']:
                pbs.logmsg(pbs.EVENT_DEBUG, "container_ports request " +
                           "not supported with Singualrity containers")
                e.reject("Port mapping feature is not supported " +
                         "for singularity")
        except AttributeError:
            pass
        self.container_image()
        is_script = False
        shell_type = {"bash", "csh", "sh", "zsh", "ksh", "tcsh", "ash"}
        for shell in shell_type:
            if e.progname.find(shell) != -1:
                is_script = True

        args = copy.deepcopy(e.argv)
        orig_prog = e.progname
        e.progname = self.singularity_cmd
        pbs.logmsg(pbs.EVENT_DEBUG4, "Container Image is: %s , \
                   Requestor is %s and Requestor host is %s" %
                   (self.cont_image,
                    pbs.event().requestor, pbs.event().requestor_host))
        e.argv = []
        e.argv.append("%s" % self.singularity_cmd)
        e.argv.append("exec")
        if 'ngpus' in str(e.job.exec_vnode):
            e.argv.append("--nv")
        e.argv.append("--bind")
        e.argv.append("%s:%s" % (self.exec_dir, self.exec_dir))
        e.argv.append("--bind")
        e.argv.append("%s:%s" % (self.home_dir, self.home_dir))
        if self.cfg["mount_jobdir"]:
            e.argv.append("--bind")
            e.argv.append("%s:%s" % (self.jobdir, self.jobdir))
        files = self.additional_binds()
        for each_file in files:
            e.argv.append("--bind")
            if type(each_file) is list:
                if len(each_file) > 1:
                    e.argv.append("%s" % ":".join(each_file))
                elif len(each_file) == 1:
                    e.argv.append("%s:%s" % (each_file, each_file))
            else:
                e.argv.append("%s:%s" % (each_file, each_file))
        # set additional exec arguments requested
        # by user using $PBS_CONTAINER_ARGS
        exec_args = self.add_args()
        if exec_args:
            for exec_arg in exec_args:
                e.argv.append(exec_arg)
        if self.cfg["mount_jobdir"]:
            # set working directory to job directory
            e.argv.append("--pwd")
            e.argv.append("%s" % self.jobdir)
        e.argv.append("--writable-tmpfs")
        if hasattr(e.job, "interactive") and e.job.interactive != None:
            e.argv.append("%s" % self.cont_image)
            e.argv.append(orig_prog)
        elif is_script == True and e.job.in_ms_mom():
            e.argv.append("%s" % self.cont_image)
            script_file = str(e.job.id) + ".SC"
            job_file = os.path.join(self.home_dir, 'mom_priv',
                                    'jobs', script_file)
            e.argv.append("%s" % job_file)
        else:
            e.argv.append("%s" % self.cont_image)
            for arg in args:
                e.argv.append(arg)
        return


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

    def __enter__(self):
        self.lockfd = open(self.path, 'w')
        fcntl.flock(self.lockfd, fcntl.LOCK_EX)
        pbs.logmsg(pbs.EVENT_DEBUG, '%s file lock acquired by %s' %
                   (self.path, str(sys._getframe(1).f_code.co_name)))

    def __exit__(self, exc, val, trace):
        if self.lockfd:
            fcntl.flock(self.lockfd, fcntl.LOCK_UN)
            self.lockfd.close()
        pbs.logmsg(pbs.EVENT_DEBUG, '%s file lock released by %s' %
                   (self.path, str(sys._getframe(1).f_code.co_name)))


#
# HPC docker job class
#
class Hpc_Docker:
    """
    This class is to handle the setup and tear
    down of the docker containers for hpc jobs
    """

    def __init__(self, conf):
        if pbs.pbs_conf['PBS_MOM_HOME']:
            self.home_dir = pbs.pbs_conf['PBS_MOM_HOME']
        else:
            self.home_dir = pbs.pbs_conf['PBS_HOME']
        self.exec_dir = pbs.pbs_conf['PBS_EXEC']
        self.jobdir = ""
        self.jid = e.job.id
        self.cont_id = e.job.id.replace("[", "_").replace("]", "_")
        self.euser = e.job.euser
        self.egroup = e.job.egroup
        self.container_image = None
        self.nvidia_docker = False
        self.image = "CONTAINER_IMAGE="
        self.user_home = ""
        self.prog = ""
        self.prog_orig = ""
        self.interactive = False
        self.container_found = False
        self.NV_GPU = ""
        self.docker_ver = self.get_docker_version()
        self.gpu_devices = None
        self.isgpujob = False
        self.cfg = conf
        self.get_docker_image()

        self.docker_cmd = self.cfg["container_cmd"]
        self.docker_exe = os.path.split(self.docker_cmd)[-1]
        self.pbs_container = os.path.join(
            self.exec_dir, "sbin", "pbs_container")

        if hasattr(e.job, "interactive") and e.job.interactive != None:
            pbs.logmsg(pbs.EVENT_DEBUG4,
                       "%s: Interactive job" % (caller_name()))
            self.interactive = True

        if 'ngpus' in str(e.job.exec_vnode):
            self.isgpujob = True
            if "nvidia_docker_cmd" in self.cfg:
                self.docker_cmd = self.cfg["nvidia_docker_cmd"]
                self.docker_exe = os.path.split(self.docker_cmd)[-1]
                self.nvidia_docker = True
            else:
                self.nvidia_docker = False

        pbs.logmsg(pbs.EVENT_DEBUG4,
                   "%s: Docker cmd: %s" %
                   (caller_name(), self.docker_cmd))

        if e.type == pbs.EXECJOB_LAUNCH:
            self.prog = e.progname
            self.prog_orig = e.progname

    def image_pull(self, container_image):
        """
        Method to pull docker image if the image is not
        already present locally.
        :param container_image: image reqested for job run
        :type container_image: string
        """
        pbs.logmsg(pbs.EVENT_DEBUG4, "%s: Method called " % (caller_name()))
        pbs.logmsg(pbs.EVENT_DEBUG, "Pulling docker image: %s" %
                   container_image)
        cmd = [self.docker_exe, "pull", container_image]
        img_err = "image pull of %s has failed" % container_image
        try:
            process = subprocess.Popen(cmd, shell=False,
                                       stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE)
        except OSError as err:
            pbs.logmsg(pbs.EVENT_DEBUG, "err: %s" % err)
            pbs.logmsg(pbs.EVENT_DEBUG, "Failed to execute: %s" %
                       ' '.join(cmd))
            e.reject(img_err)
        except ValueError as err:
            pbs.logmsg(pbs.EVENT_DEBUG, "err: %s" % err)
            pbs.logmsg(pbs.EVENT_DEBUG, "Failed to execute: %s" %
                       ' '.join(cmd))
            pbs.logmsg(pbs.EVENT_DEBUG, "Invalid arguments passed.")
            e.reject(img_err)
        return process

    def map_vnodes_to_momhosts(self):
        """
        Look through the list of vnodes managed by the server
        and cache the Mom attribute values in a dictionary
        vn_momhosts.
        returns: Dictionary
        """
        pbs.logmsg(pbs.EVENT_DEBUG4, "%s: Method called " % (caller_name()))
        vn_momhosts = {}
        vns = pbs.server().vnodes()
        for v in vns:
            vn_momhosts[v.name] = v.Mom
        return vn_momhosts

    def add_resource_restriction(self):
        """
        Goes through the chunklist and adds resource
        restriction on the particular Vnode
        returns: list
        """
        pbs.logmsg(pbs.EVENT_DEBUG4, "%s: Method called " % (caller_name()))
        pbs.logmsg(pbs.EVENT_DEBUG4, "job %s exec_host2 = %s" %
                   (e.job.id, e.job.exec_host2))
        pbs.logmsg(pbs.EVENT_DEBUG4, "job %s exec_vnode = %s" %
                   (e.job.id, e.job.exec_vnode))
        local_hostname = socket.gethostname()
        local_hostname_short = local_hostname.split('.')[0]
        ncpus = 0
        mem = 0
        chunklist = e.job.exec_vnode.chunks
        vn_momhosts = self.map_vnodes_to_momhosts()
        for chunk in chunklist:
            vnode = e.vnode_list[chunk.vnode_name]
            if (vn_momhosts[chunk.vnode_name] == local_hostname) or \
                    (vn_momhosts[chunk.vnode_name] == local_hostname_short):
                for res in chunk.chunk_resources.keys():
                    pbs.logmsg(pbs.EVENT_DEBUG4, "c.chunk_resources[%s]=%s" %
                               (res, chunk.chunk_resources[res]))
                    if res == "ncpus":
                        ncpus = ncpus + chunk.chunk_resources[res]
                    elif res == "mem":
                        chunk_mem = str(chunk.chunk_resources[res])[0:-2]
                        mem = mem + int(chunk_mem)

        ret_list = []
        if ncpus:
            ret_list += ["--cpus", str(ncpus)]
        if mem:
            ret_list += ["-m", str(mem) + "kb"]
        return ret_list

    def get_docker_version(self):
        """
        Method to get nvidia-docker version inorder
        to achive GPU isolation.
        returns: int
        """
        pbs.logmsg(pbs.EVENT_DEBUG4, "%s: Method called " % (caller_name()))
        if self.nvidia_docker:
            cmd = ["nvidia-docker", "version"]
            output = exec_cmd(cmd)
            if output == None:
                return 0
            lines = output.split('\n')
            version = (lines[0].split(':'))[1].split('.')
            version = version[0].lstrip()
            return int(version)
        else:
            cmd = ["docker", "version"]
            output = exec_cmd(cmd)
            if output == None:
                return 0
            lines = output.split('\n')
            version = (lines[1].split(':'))[1].split('.')
            version = version[0].lstrip() + version[1].lstrip()
            return int(version)


    def add_env(self):
        """
        Method to Set environment variables to the container
        returns: list
        """
        pbs.logmsg(pbs.EVENT_DEBUG4, "%s: Method called " % (caller_name()))
        # Remove environment variables that are known to cause trouble
        ret_list = []
        for key in self.cfg["remove_env_keys"]:
            if key in e.env:
                del e.env[key]

        for i, v in e.env.items():
            if i == 'PBS_JOBDIR':
                self.jobdir = v
            if self.nvidia_docker:
                nvidia_docker_ver = self.get_docker_version()
                if i == 'CUDA_VISIBLE_DEVICES':
                    v = v.replace('\\,', ',')
                    if nvidia_docker_ver == 1:
                        self.NV_GPU = v
                    elif nvidia_docker_ver == 2:
                        ret_list += ["-e", "NVIDIA_VISIBLE_DEVICES=" + v]
                elif i == 'OFFLOAD_DEVICES':
                    v = v.replace('\\,', ',')
            elif self.isgpujob:
                if i == 'CUDA_VISIBLE_DEVICES':
                    v = v.replace('\\,', ',')
                    if self.docker_ver < 1903:
                        ret_list += ["-e", "NVIDIA_VISIBLE_DEVICES=" + v]
                    else:
                        self.gpu_devices = v
            ret_list += ["-e", str(i) + "=" + v]
        if self.jobdir == None or self.jobdir == "":
            pbs.logmsg(pbs.EVENT_DEBUG,
                       "Staging directory not set in PBS_JOBDIR")
            e.reject("PBS_JOBDIR is not set")
        return ret_list

    def get_docker_image(self):
        """
        Method to get docker image for the container
        """
        pbs.logmsg(pbs.EVENT_DEBUG4, "%s: Method called " % (caller_name()))
        try:
            if e.job.Resource_List['container_image']:
                self.container_image = e.job.Resource_List['container_image']
        except AttributeError:
            # for backward compatibility
            pass
        varlist = str(e.job.Variable_List)
        vlist = re.split(r'(?<!\\),', varlist)
        for i in vlist:
            pbs.logmsg(pbs.EVENT_DEBUG4, "Variable is: %s" % i)
            if self.container_image == None:
                if i.startswith(self.image):
                    name = i.split("=")
                    self.container_image = name[1]
            if i.startswith("PBS_O_HOME="):
                name = i.split("=")
                self.user_home = name[1]
        return

    def port_mapping(self, cmd):
        """
        Method for exposing port(s) requested for the job,
        and map it to a host port.
        :param cmd: docker run command
        :type cmd: list
        """
        pbs.logmsg(pbs.EVENT_DEBUG4, "%s: Method called " % (caller_name()))
        expose_port = []
        cmd_insp = [self.docker_exe, "inspect",
                    "--format=\'{{.ContainerConfig.ExposedPorts}}\'",
                    str(self.container_image)]
        pbs.logmsg(pbs.EVENT_DEBUG4, "cmd: %s" %
                                     " ".join(cmd_insp))
        out = exec_cmd(cmd_insp)
        # An example for 'out':
        # map[80/tcp:{} 803/tcp:{}]
        # where both 80 and 803 are the ports to be exposed
        # in the container.
        port_exp = out.strip().split("map")[1]
        port_exp = port_exp.split(" ")
        for port in port_exp:
            pre_exp = list(filter(str.isdigit, port))
            if pre_exp:
                expose_port += ["".join(pre_exp)]
        try:
            if e.job.Resource_List['container_ports']:
                port_inp = \
                    str(e.job.Resource_List['container_ports']).split(",")
                expose_port += port_inp
        except AttributeError:
            pbs.logmsg(pbs.EVENT_DEBUG4, "No ports requested")
        if expose_port:
            # argument passed to docker cmd for port mapping
            # is of the format: -p [port_range]:expose_port.
            for port in expose_port:
                port_map = ":" + str(port)
                if self.cfg["port_ranges"]:
                    port_range = self.cfg["port_ranges"][0]
                    port_map = str(port_range) + port_map
                cmd += ["-p", port_map]
        return

    def update_ports_used(self, container_id):
        """
        Update container_ports resource value indicating port
        mapping between a chosen host port with the exposed
        ports in the container, with a ':' separator.
        :type container_id: string
        """
        cmd = [self.docker_exe, "port", container_id]
        output = exec_cmd(cmd)
        # An example for value of 'output':
        # 80/tcp -> 0.0.0.0:32768
        # 80 is the port exposed in the container and
        # 32768 is the host port it is mapped to.
        if output:
            job_port = []
            port_map = (output.rstrip()).split("\n")
            for port in port_map:
                host_port = port.split(":")[1].strip()
                cont_port = port.split("/")[0].strip()
                port_usd = cont_port + ":" + host_port
                job_port += [port_usd]
            port = ",".join(job_port)
            e.job.resources_used['container_ports'] = str(port)
        return

    def user_group_list(self):
        """
        Method to identify the user's groups on the node
        and get the user into all of them inside the container.
        returns: list
        """
        pbs.logmsg(pbs.EVENT_DEBUG4, "%s: Method called " % (caller_name()))
        groups = [g.gr_gid for g in grp.getgrall() if self.euser in g.gr_mem]
        return groups

    def docker_login(self, auth_data, reg):
        """
        Method for logging into a docker registry for authetication
        for using private images from its repository
        :param auth_data: user authetication info
        :type auth_data: dict
        :param reg: container registry string from user
        container image request
        :type reg: string
        """
        pbs.logmsg(pbs.EVENT_DEBUG4, "%s: Method called " % (caller_name()))
        if not auth_data['passwd']:
            # skip login
            msg = "No value set for passwd in .container/tokens.json file"
            pbs.logmsg(pbs.EVENT_DEBUG, msg)
            pbs.logmsg(pbs.EVENT_DEBUG,
                       "Skipping Container login")
            return

        if not auth_data['user_id']:
            # use job owner id
            auth_data['user_id'] = self.euser

        cmd = [self.docker_exe, "login", reg,  "--username",
               auth_data['user_id'], "--password-stdin"]
        pbs.logmsg(pbs.EVENT_DEBUG4, "Docker login cmd: %s" %
                   " ".join(cmd))
        try:
            process = subprocess.Popen(cmd, shell=False,
                                       stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE)
            process.stdin.write((auth_data['passwd']).encode('utf-8'))
            output, err = process.communicate()
            process.stdin.close()
        except OSError as err:
            pbs.logmsg(pbs.EVENT_DEBUG, "err: %s" % err)
            pbs.logmsg(pbs.EVENT_DEBUG, "Failed to execute: %s" %
                       ' '.join(cmd))
            pbs.logmsg(pbs.EVENT_DEBUG,
                       "Skipping Container login")
        except ValueError as err:
            pbs.logmsg(pbs.EVENT_DEBUG, "err: %s" % err)
            pbs.logmsg(pbs.EVENT_DEBUG, "Failed to execute: %s" %
                       ' '.join(cmd))
            pbs.logmsg(pbs.EVENT_DEBUG, "Invalid arguments passed.")
            pbs.logmsg(pbs.EVENT_DEBUG,
                       "Skipping Container login")
        if process.returncode != 0:
            pbs.logmsg(pbs.EVENT_DEBUG,
                       "Unable to run command: %s err: %s returncode: %d" %
                       (' '.join(cmd), err, process.returncode))
            e.reject("Container login Failed")
        pbs.logmsg(pbs.EVENT_DEBUG, "%s" % output)
        pbs.logmsg(pbs.EVENT_DEBUG, "%s;%s successfully logged in to %s" %
                   (self.jid, auth_data['user_id'], reg))
        return

    def docker_logout(self, reg, user):
        """
        Method to logout from the reg.
        :param reg: container registry string from user
        container image request
        :type reg: string
        :param user: repository user logging out in registry
        :type user: string
        """
        pbs.logmsg(pbs.EVENT_DEBUG4, "%s: Method called " % (caller_name()))
        if not user:
            user = self.euser
        if reg == "docker.io":
            # if we do not specify a registry in docker
            # logout command then logout happens from default registry,
            # which is docker hub
            cmd = [self.docker_exe, "logout"]
        else:
            cmd = [self.docker_exe, "logout", reg]
        out = exec_cmd(cmd)
        if out == None:
            e.reject("Container logout Failed")
        msg = "%s successfully logged out from container registry %s" % \
            (user, reg)
        pbs.logmsg(pbs.EVENT_DEBUG, msg)
        return

    def docker_security_check(self):
        """
        Method to check built in image access controls
        using job submitter's registry login data
        """
        pbs.logmsg(pbs.EVENT_DEBUG4, "%s: Method called " % (caller_name()))
        # lock file path
        docker_lock_file = os.path.join(self.home_dir, 'mom_priv',
                                        'docker.lock')
        auth_data = get_auth_param(self.cfg)
        # extract registry url from container_image
        reg = ""
        # set default_reg to the first element of allowed_registries list
        default_reg = self.cfg["allowed_registries"][0]

        image_list = self.container_image.split('/')
        # check if image_list[0] is present in allowed_registries
        if image_list[0] in self.cfg["allowed_registries"]:
            reg = image_list[0]
        else:
            if len(image_list) > 2:
                # allowed_registries must have PBS_ALL
                reg = image_list[0]
            else:
                reg = default_reg
                msg = "repo value %s could not be validated." % \
                    image_list[0]
                msg += " Using default repo %s" % default_reg
                pbs.logmsg(pbs.EVENT_DEBUG, "%s" % msg)
                self.container_image = reg + '/' + self.container_image

        if reg not in auth_data:
            auth_data[reg] = {}
            auth_data[reg]['user_id'] = self.euser
            auth_data[reg]['passwd'] = ""

        # lock acquire
        with Lock(docker_lock_file):
            # Do a docker logout before docker login for security
            self.docker_logout(reg, auth_data[reg]['user_id'])
            self.docker_login(auth_data[reg], reg)
            process = self.image_pull(self.container_image)
            self.docker_logout(reg, auth_data[reg]['user_id'])
        output, err = process.communicate()
        if process.returncode != 0:
            if 'pull access denied' in err.decode('utf-8'):
                msg = "User is not authorized or repository does not exist"
                pbs.logmsg(pbs.EVENT_DEBUG, msg)
            pbs.logmsg(pbs.EVENT_DEBUG, "err: %s" % err)
            e.reject("Container pull of image %s has failed" %
                     self.container_image)
        pbs.logmsg(pbs.EVENT_DEBUG,
                   "Container image %s pulled sucessfully" %
                   self.container_image)
        return

    def create_container(self, cgroup_enabled):
        """
        Method to create a container corresponding to
        its job with its name same as the job ID and
        configure the container with required settings
        """
        pbs.logmsg(pbs.EVENT_DEBUG4, "%s: Method called " % (caller_name()))
        # check to see if the container is already running
        search_cmd = [self.docker_exe, "ps", "-q",
                      "-f", "name=" + str(self.cont_id)]
        pbs.logmsg(pbs.EVENT_DEBUG4, "Search cmd: %s" %
                   " ".join(search_cmd))
        out = exec_cmd(search_cmd)
        if out == None:
            e.reject("Create container failed for %s" % self.cont_id)
        if out.strip():
            self.container_found = True
            pbs.logmsg(pbs.EVENT_DEBUG4, "Docker container %s found!" %
                       str(self.cont_id))
        else:
            pbs.logmsg(
                pbs.EVENT_DEBUG4,
                "Container Image is: %s , \
                Requestor is %s and Requestor host is %s" %
                (self.container_image,
                 pbs.event().requestor, pbs.event().requestor_host))
            self.docker_security_check()
            cmd = []
            cmd += [self.docker_exe, "run", "-d", "-it"]
            cmd += ["--name", str(self.cont_id)]
            cmd += ["--rm"]
            cmd += ["-h", socket.getfqdn()]
            cmd += ["--user=" + str(pwd.getpwnam(self.euser).pw_uid) + ":" +
                    str(pwd.getpwnam(self.euser).pw_gid)]
            if self.cfg["enable_group_add_arg"]:
                groups = self.user_group_list()
                for gid in groups:
                    cmd += ["--group-add=" + str(gid)]
            if cgroup_enabled:
                cgroup_parent_dir = "pbs_jobs.service/jobid/" + self.jid
                cmd += ["--cgroup-parent=" + cgroup_parent_dir]
            else:
                cmd += self.add_resource_restriction()
            if cgroup_enabled:
                if self.isgpujob and self.nvidia_docker == False and self.docker_ver < 1903:
                    cmd += ["--runtime=nvidia"]
            cmd += self.add_env()
            if cgroup_enabled:
                if self.isgpujob and self.nvidia_docker == False and self.docker_ver >= 1903:
                    devices =  '"' + "device=" + str(self.gpu_devices) + '"'
                    pbs.logmsg(pbs.EVENT_DEBUG, "devices: %s" % devices)
                    cmd += ["--gpus", str(devices)]
            files_to_mount = []
            files_to_mount.append(self.home_dir)
            files_to_mount.append(self.exec_dir)
            files_to_mount.append(self.user_home)
            if self.cfg["mount_jobdir"]:
                files_to_mount.append(self.jobdir)
            # mount pbs.conf file
            conf_path = os.path.join(os.sep + "etc",
                                     "pbs.conf")
            files_to_mount.append(conf_path)

            # Steps for stagein and stageout request into the container
            if e.job.stagein:
                stagein_file = str(e.job.stagein).split(",")
                for f in stagein_file:
                    destin = f.split("@")[0]
                    if not os.path.isabs(destin):
                        destin = os.path.join(self.jobdir, destin)
                    files_to_mount.append(destin)

            if e.job.stageout:
                source = str(e.job.stageout).split("@")[0]
                if os.path.isabs(source):
                    if os.path.exists(source):
                        pbs.logmsg(pbs.EVENT_DEBUG4,
                                   "Stageout Path Exists: %s" % source)
                    else:
                        while os.path.exists(source) == False:
                            source = os.path.split(source)[0]
                        if source == "/":
                            pbs.logmsg(pbs.EVENT_DEBUG,
                                       "Stageout path does not exist!")
                            e.reject("Invalid bind mount")
                        pbs.logmsg(pbs.EVENT_DEBUG4,
                                   "new Stageout path : %s" % source)
                else:
                    pbs.logmsg(pbs.LOG_DEBUG, "Jobdir :%s" %
                               (e.env['PBS_JOBDIR']))
                    source = str(e.env['PBS_JOBDIR'])
                files_to_mount.append(source)

            # remove duplicate values
            final_mount = list(dict.fromkeys(files_to_mount))
            for fi in final_mount:
                cmd += ["--mount", "type=bind,source=" + str(fi) +
                        ",target=" + str(fi)]

            # add additional mount paths from config file
            for key in self.cfg["mount_paths"]:
                if type(key) is list:
                    if key[0] in final_mount:
                        continue
                    # if len(key) is >=3 then the value has
                    # source,dest and some additional options
                    if len(key) >= 3:
                        final_mount.append(key[0])
                        cmd += ["--mount", "type=bind,source=" + key[0] +
                                ",target=" + key[1] + "," + ','.join(key[2:])]
                    # if len(key) is 2 then the value has source and dest
                    if len(key) == 2:
                        final_mount.append(key[0])
                        cmd += ["--mount", "type=bind,source=" + key[0] +
                                ",target=" + key[1]]
                    else:
                        pbs.logmsg(pbs.EVENT_DEBUG,
                                   "Cannot mount %s\n invalid input" % key)
                else:
                    if key in final_mount:
                        continue
                    final_mount.append(key)
                    cmd += ["--mount", "type=bind,source=" + key +
                            ",target=" + key]
            # add extra docker run arguments requested in $PBS_CONTAINER_ARGS
            allowed = self.cfg["container_args_allowed"]
            if allowed:
                for i, arg in enumerate(allowed):
                    # arguments can start with a single '-' or double '--',
                    # irrespective of that, we need just the value for
                    # comparing with user input in $PBS_CONTAINER_ARGS
                    allowed[i] = arg.strip("-")
                try:
                    if e.env['PBS_CONTAINER_ARGS']:
                        # mulitple arguments are ';' separated.
                        args = e.env['PBS_CONTAINER_ARGS'].split(";")
                        for arg in args:
                            # arguments receive values either
                            # with a equal sign or a space
                            if arg.split("=")[0].strip("-") in allowed:
                                # for values having ',' in them,
                                # PBS adds escape char '\'
                                cmd += [arg.replace("\\", "")]
                            elif arg.split(" ")[0].strip("-") in allowed:
                                cmd += [arg.replace("\\", "")]
                            else:
                                msg = "%s not listed in " \
                                    "container_args_allowed in Config file." \
                                    " Please contact Admin" % arg
                                pbs.logmsg(pbs.EVENT_DEBUG, msg)
                except KeyError:
                    pass
            chunklist = e.job.exec_vnode.chunks
            hostname = pbs.get_local_nodename()
            try:
                if all(chunk.vnode_name == hostname for chunk in chunklist):
                    if len(chunklist) == 1:
                        self.port_mapping(cmd)
                    else:
                        # for multi-chunk jobs
                        cmd += ["--net=host"]
                elif e.job.Resource_List['container_ports']:
                    msg = \
                        "container port mapping allowed only " \
                        "for single node jobs"
                    e.reject(msg)
                else:
                    cmd += ["--net=host"]
            except AttributeError:
                cmd += ["--net=host"]
            path = os.path.join(self.exec_dir, 'bin', 'pbs_sleep')
            cmd += ["--entrypoint", path]
            cmd += [str(self.container_image),  "-1"]
            pbs.logmsg(pbs.EVENT_DEBUG4, "Cmd is: %s" % " ".join(cmd))
            environ = os.environ
            if self.NV_GPU != '':
                environ['NV_GPU'] = self.NV_GPU
            try:
                process = subprocess.Popen(cmd, shell=False, env=environ,
                                           stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE)
                out, err = process.communicate()
            except OSError:
                pbs.logmsg(pbs.EVENT_DEBUG, "Failed to execute: %s" %
                           ' '.join(cmd))
                return
            except ValueError:
                pbs.logmsg(pbs.EVENT_DEBUG, "Failed to execute: %s" %
                           ' '.join(cmd))
                pbs.logmsg(pbs.EVENT_DEBUG, "Invalid arguments passed.")
                return
            status = process.returncode
            if status != 0:
                if status == 125:
                    sp_err = err.decode('utf-8').split(":")
                if any("all ports are allocated." in s.strip() for s in sp_err):
                    pbs.logmsg(pbs.EVENT_DEBUG,
                               "No free ports available in range: %s, %s" %
                               (self.cfg["port_ranges"][0],
                                sp_err[-1].strip()))
                    del self.cfg["port_ranges"][0]
                    if self.cfg["port_ranges"]:
                        self.create_container(cgroup_enabled)
                        return
                    else:
                        e.reject("Unable to run docker container: %s" %
                                 sp_err[-1].strip())
                pbs.logmsg(pbs.EVENT_DEBUG, "stderr: %s" % err.decode('utf-8'))
                e.reject("Unable to run docker container")
            pbs.logmsg(pbs.EVENT_DEBUG4, "Container ID: %s" % out)
            self.update_ports_used(str(self.cont_id))

        return

    def launch_job(self, args, cgroup_enabled):
        """
        Method to launch a job inside the container
        """
        pbs.logmsg(pbs.EVENT_DEBUG4, "%s: Method called " % (caller_name()))

        is_script = False
        shell_type = {"bash", "csh", "sh", "zsh", "ksh", "tcsh", "ash"}
        for shell in shell_type:
            if self.prog.find(shell) != -1:
                is_script = True

        e.progname = self.pbs_container
        e.argv = []
        e.argv.append("%s" % self.pbs_container)
        e.argv.append("%s" % e.job.id)
        e.argv.append("%s" % cgroup_enabled)
        e.argv.append("%s" % self.exec_dir)
        e.argv.append("exec")
        if self.cfg["mount_jobdir"]:
            e.argv.append("-w")
            e.argv.append(str(self.jobdir))
        # For interactive jobs
        if(self.interactive and self.container_found == False and
           e.job.in_ms_mom()):
            e.argv.append("-it")
            e.argv.append(str(self.cont_id))
            e.argv.append(self.prog_orig)

        elif is_script == True and e.job.in_ms_mom():
            script_file = str(e.job.id) + ".SC"
            job_file = os.path.join(self.home_dir, 'mom_priv',
                                    'jobs', script_file)
            e.argv.append(str(self.cont_id))
            e.argv.append(self.prog_orig)
            e.argv.append("-c")
            e.argv.append(job_file)

        else:
            e.argv.append(str(self.cont_id))
            for arg in args:
                e.argv.append(arg)

        return

    def shutdown_job(self):
        """
        Method to stop the container after job completion
        """
        pbs.logmsg(pbs.EVENT_DEBUG4, "%s:Method called " % (caller_name()))
        # check if the container has been cleaned up already.
        container_name = "name=" + self.cont_id
        cmd = [self.docker_exe, "ps", "--filter",
               container_name, "--quiet", "--no-trunc"]
        output = exec_cmd(cmd)
        if output == None:
            return
        if output == "":
            pbs.logmsg(pbs.EVENT_DEBUG4, "Container already cleaned up.")
        else:
            cmd = [self.docker_exe, "stop", self.cont_id]
            out = exec_cmd(cmd)
            if out == None:
                return
        return


def main():
    pbs.logmsg(pbs.EVENT_DEBUG4, "%s: Function called" % (caller_name()))
    hostname = pbs.get_local_nodename()
    pbs.logmsg(pbs.EVENT_DEBUG4, "%s: Host is %s" % (caller_name(), hostname))
    pbs.logmsg(pbs.EVENT_DEBUG4, "%s: Hook name is %s" %
               (caller_name(), e.hook_name))

    try:
        # Instantiate the hook utility class
        hooks = HookUtils()
        pbs.logmsg(pbs.EVENT_DEBUG4, "%s: Event type is %s" %
                   (caller_name(), hooks.event_name(e.type)))
        pbs.logmsg(pbs.EVENT_DEBUG4, "%s: Hook utility class instantiated" %
                   (caller_name()))
    except:
        pbs.logmsg(pbs.EVENT_DEBUG,
                   "%s: Failed to instantiate hook utility class" %
                   (caller_name()))
        pbs.logmsg(pbs.EVENT_DEBUG,
                   str(traceback.format_exc().strip().splitlines()))
        e.accept()
    if not hooks.hashandler(e.type):
        # Bail out if there is no handler for this event
        pbs.logmsg(pbs.EVENT_DEBUG, "%s: %s event not handled by this hook" %
                   (caller_name(), hooks.event_name(e.type)))
        e.accept()
    conf = parse_config_file()
    cgroup_enabled = 1
    if pbs.pbs_conf['PBS_MOM_HOME']:
        hdir = pbs.pbs_conf['PBS_MOM_HOME']
    else:
        hdir = pbs.pbs_conf['PBS_HOME']
    job_file = os.path.join(hdir, "mom_priv", "hooks", "pbs_cgroups.HK")
    if os.path.exists(job_file):
        with open(job_file, "r") as f:
            for line in f:
                if "enabled=false" in line:
                    cgroup_enabled = 0
        # cgroup hook is enabled but the node is included in exclude_host
        if cgroup_enabled:
            cg = Cgroups(conf, cgroup_enabled)
            # cgroup hook is enabled but no cgroups mounted on the host
            if not cg.cgroups_paths:
                cgroup_enabled = 0
            if not os.path.exists(cg.cgroups_paths['memory']):
                cgroup_enabled = 0
    else:
        pbs.logmsg(pbs.EVENT_DEBUG4, "Unable to open file: %s" %
                   job_file)
        cgroup_enabled = 0
    container_job = 0
    if e.type != pbs.EXECHOST_PERIODIC:
        try:
            if e.job.Resource_List['container_image']:
                container_job = 1
        except AttributeError:
            # for backward compatibility
            pass
        if container_job == 0:
            varlist = str(e.job.Variable_List)
            vlist = re.split(r'(?<!\\),', varlist)
            for i in vlist:
                if i.startswith("CONTAINER_IMAGE="):
                    container_job = 1
        if not container_job:
            e.accept("Not a Container Job")
    if 'container_resource_name' not in conf.keys() or \
            not conf['container_resource_name']:
        e.reject("config parameter container_resource_name is empty/None")
    if hooks.invoke_handler(e.type, conf, cgroup_enabled) is True:
        pbs.logmsg(pbs.EVENT_DEBUG4, "%s: Hook handler returned success" %
                   (caller_name()))
        e.accept()
    else:
        pbs.logmsg(pbs.EVENT_DEBUG, "%s: Hook handler returned failure" %
                   (caller_name()))
        e.reject()


try:
    main()
except Exception as exc:
    # Catch all other exceptions and report them.
    pbs.logmsg(pbs.EVENT_DEBUG, str(
        traceback.format_exc().strip().splitlines()))
    msg = ("Unexpected error in %s handling %s event" % (e.hook_name, e.type))
    msg += (": %s %s" % (exc.__class__.__name__, str(exc.args)))
    pbs.logmsg(pbs.EVENT_ERROR, msg)
e.reject(msg)

