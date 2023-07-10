"""Validate that a job has certain fields set, forbid others.

To set:
create hook validate_qsub
import hook validate_qsub x-application/python default validate_qsub.py
import hook validate_qsub x-application/config default validate_qsub.json
set hook validate_qsub 'queuejob,modifyjob'

"""
import json
import traceback
from typing import Any, Dict, List, Tuple, Set
import pbs

FORBIDDEN_RESOURCES = "forbidden_resources"
REQUIRED_RESOURCES = "required_resources"
MANAGER_ALLOW = "manager_allow"
OPERATOR_ALLOW = "operator_allow"
FORBIDDEN_ATTRIBUTES = "forbidden_attributes"
REQUIRED_ATTRIBUTES = "required_attributes"
DEFAULT_ATTRIBUTE_OVERRIDE = "default_attribute_override"
# current as of v2021
DEFAULT_ATTRIBUTE_VALUES = {"Checkpoint": "u",
                            "Keep_Files": "n",
                            "Mail_Points": "a",
                            }

# check to make sure a user did or did not include a resource
default_config = {FORBIDDEN_RESOURCES: [],
                  REQUIRED_RESOURCES: [],
                  MANAGER_ALLOW: [],
                  OPERATOR_ALLOW: [],
                  FORBIDDEN_ATTRIBUTES: [],
                  REQUIRED_ATTRIBUTES: [],
                  DEFAULT_ATTRIBUTE_OVERRIDE: {}}


def get_config(default_config: Dict[str, Any]) -> Dict:
    config = {}
    config.update(default_config)
    if pbs.hook_config_filename is not None:
        with open(pbs.hook_config_filename) as f:
            config.update(json.loads(f.read()))
    extra_keys = set(config.keys()) - set(default_config.keys())
    if extra_keys:
        raise ValueError(f"Invalid keys found: {extra_keys}")
    return config


def parse_acl(acl_val: str) -> List[str]:
    '''parse an acl value from PBS, generate a dict of:
        {username: [<hosts], ...} for each username in the string.

    '''
    if acl_val is None:
        return []
    return [s.split('@')[0] for s in str(acl_val).split(',')]


def elevated_user(user: str, server: Any) -> Tuple[bool, bool]:
    '''Check to see if a requestor is in the operator or manager group'''
    managers = parse_acl(server.managers)
    operators = parse_acl(server.operators)
    return user in managers, user in operators


def form_forbidden_set(config_set: Set[str], config: Dict[str, Any],
                       operator: bool, manager: bool) -> set:
    return_set = set()
    return_set.update(config_set)
    if operator:
        return_set -= set(config[OPERATOR_ALLOW])
    if manager:
        return_set -= set(config[MANAGER_ALLOW])
    return return_set


def check_resources(resource_list: Dict[str, Any], config: Dict[str, Any], event: Any, manager: bool = False,
                    operator: bool = False) -> Tuple[bool, str]:
    '''take a select and find the resources from the config that are
    required/forbidden.

    rejects if there are bad resources.

    Returns:
        request_ok: True if request information is ok, False if something failed
        msg: string describing failed flag/resource

    '''
    request_ok = True
    msg = ""
    if event.type == pbs.QUEUEJOB:
        for resource in config[REQUIRED_RESOURCES]:
            # This has to be handled in a bit of an odd way
            # resource_list is a dict that isn't really a dict.
            if (resource not in resource_list.keys() and
                    resource_list[resource] is None):
                request_ok = False
                msg = f"Resource: {resource} is required to be set."
                return request_ok, msg
    forbidden_resources = form_forbidden_set(set(config[FORBIDDEN_RESOURCES]), config, operator, manager)
    for resource in forbidden_resources:
        if (resource in resource_list.keys() and resource_list[resource] is not None):
            request_ok = False
            msg = f"Resource: {resource} cannot be set by qsub."
    return request_ok, msg


def check_job_req(job: Any, config: Dict[str, Any], event: Any,
                  manager: bool = False, operator: bool = False) -> Tuple[bool, str]:
    '''Given a job and the config, check to see if all attributes and resources
    are included if required, and are not forbidden. Checks attributes first,
    then does a check through the Resource_List.

    Returns:
        Tuple of a boolean if the check passed, and a string message to use
        in the event the job request here is to be rejected

    '''
    if event.type == pbs.QUEUEJOB:
        for attr in config[REQUIRED_ATTRIBUTES]:
            if getattr(job, attr, None) is None:
                # This request already failed check
                return False, f"{attr} is required to be set."
    forbidden_attrs = form_forbidden_set(set(config[FORBIDDEN_ATTRIBUTES]), config, operator, manager)
    DEFAULT_ATTRIBUTE_VALUES.update(config[DEFAULT_ATTRIBUTE_OVERRIDE])
    for attr in forbidden_attrs:
        if getattr(job, attr, None) is not None:
            # String coercion because PBS usually gives back unexpected types
            if set(str(getattr(job, attr))) ^ set(str(DEFAULT_ATTRIBUTE_VALUES.get(attr, None))) != set():
                return False, f"{attr} cannot be set by qsub."
    # if we passed the general attributes then check specific resources in the
    # Resource_List for things like walltime and preemption parameters.
    return check_resources(event.job.Resource_List, config, event, manager,
                           operator)


event = pbs.event()  # If this blows up the below exception handler is a moot point.
try:
    server = pbs.server()
    user = event.requestor
    config = get_config(default_config)
    manager, operator = elevated_user(user, server)
    request_ok, msg = check_job_req(event.job, config, event, manager, operator)
    if request_ok:
        event.accept()
    else:
        event.reject(msg)

except Exception as ex:
    pbs.logmsg(pbs.LOG_ERROR, f"Exception: {ex}\n{str(traceback.format_exc().strip().splitlines())}")
    event.reject("Exception in validate_qsub hook encountered.  Please contact your administrator.")
