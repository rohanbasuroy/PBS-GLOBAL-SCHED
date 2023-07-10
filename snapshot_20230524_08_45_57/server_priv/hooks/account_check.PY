#!/bin/python3
import pbs
import sys
import json
import time
import traceback
from typing import Any, Dict, List, Tuple


# Overburn entry formats are: a dict consisting of:
#   key is the name of the category, type of project, entries in the dict are:
#       start_month -- Month to start permitting overburn in
#       end_month -- Month to stop permitting overburn after
#       ratio -- Overburn ratio, how far over the project's allocation
#
# Priority:
#   Project > award_type > award_category  More specific wins.
#
# Duration:
#   start_month and end_month define a range.  This range wraps around
#   for the end of the year, so a range of 11 to 2 would cover months 11, 12,
#   1, and 2, inclusive.  A range of 5 to 7 would give 5, 6, 7 as active months
#   as expected.

default_config = {"aux_lib_paths": ["/usr/local/lib/python3.6/site-packages",
                                    "/usr/lib/python3.6/site-packages"],
                  "default_ni_resource": "polaris",
                  "default_project_priority": 2.0,
                  "ignore_negative_balance": [],
                  "award_type": {},
                  "award_category": {},
                  "project_override": {}
                  }


project_info_fields = {"overburn_start_month",
                       "overburn_end_month",
                       "overburn_ratio",
                       "project_priority"
                       }


# Internal users: this hook should ignore these users.
pbs_internal_user_list = ["PBS_Server", "Scheduler", "pbs_mom", "root"]


def get_config(default_config: Dict[str, Any]) -> Dict:
    '''provide basic validation of keys for config file'''
    config = {}
    config.update(default_config)
    if pbs.hook_config_filename is not None:
        with open(pbs.hook_config_filename) as f:
            config.update(json.loads(f.read()))
    extra_keys = set(config.keys()) - set(default_config.keys())
    if extra_keys:
        raise ValueError(f"Invalid keys found: {extra_keys}")
    # Check sub keys for project information:
    for k, v in config.items():
        if k in ["award_type", "award_category", "project_override"]:
            # Need to go a level deeper
            for j in v.keys():
                extra_keys = set(v[j].keys()) - project_info_fields
                if extra_keys:
                    raise ValueError(f"Invalid keys found in {k}[{j}]: {extra_keys}")
    return config


# Ni needs a special import here due to system dependent lib location
try:
    config = get_config(default_config)
    for aux_lib_path in config["aux_lib_paths"]:
        if aux_lib_path not in sys.path:
            sys.path.append(aux_lib_path)
    pbs.logmsg(pbs.LOG_DEBUG, f"sys.path now: {sys.path}")
    from ni.site import api as sapi
    from ni.site.constants import ErrorCode
except Exception as ex:
    pbs.logmsg(pbs.LOG_ERROR, f"Error in accounting import: {str(ex)}")
    pbs.logmsg(pbs.LOG_DEBUG, f"Error information from hook:\n{traceback.format_exc()}")
    pbs.event().reject("An error occurred in setting up the accounting system.  Please contact support.")


event = pbs.event()
server = pbs.server()

# There are some users that this should never apply to:
if event.requestor in pbs_internal_user_list:
    event.accept()


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


manager, operator = elevated_user(event.requestor, server)
if (manager or operator) and event.type == pbs.MODIFYJOB and event.job.Account_Name is None:
    # This allows managers and operators to qalter jobs with this hook in place unless project is getting altered
    event.accept()


def get_queue_params(server, job_queue, resource,
                     attribute="resources_default", default=None):
    '''Get parameters for the queue, by querying the overall server
    for defaults first, then applying queue-specific parameters

    Returns:
        The value held by the highest priority of default, server or queue.
        The priority is default < server < queue

    '''
    # if these are unset, the entry in server will return None
    value = default
    server_value = getattr(server, attribute)[resource]
    if server_value is not None:
        value = server_value
    queue = server.queue(job_queue.name)
    if queue is None:
        # User may have specified a queue that doesn't exist.
        # That validation happens later in PBS itself.
        return value
    queue_value = getattr(queue, attribute)[resource]
    if queue_value is not None:
        value = queue_value
    return value


def in_overburn_date_range(start, end):
    ''''Return if we are within the specified overburn range, this "wraps around"
    the calendar, so a start-end of something like 11, 2 would run from Nov to
    Feb of the next year.  Otherwise it's a simple check of are we in the range.

    start: starting month for overburn
    end: ending month of overburn

    Note: month range is inclusive

    '''
    in_range = False
    month = time.gmtime().tm_mon
    if start < end:
        if start <= month and end >= month:
            in_range = True
    else:
        if start <= month or end >= month:
            in_range = True
    return in_range


def case_insensitive_config_find(config_dict, search_key, default=None):
    retval = default
    for key in config_dict.keys():
        if key.lower() == search_key.lower():
            retval = key
    return retval


def get_project_config_info(job: Any, account_info: Dict[str, Any]) ->  Dict:
    '''Get info from award_category, award_name and project info as appopraite
    from the supplied config.

    '''
    config_info = {}
    project_name = case_insensitive_config_find(config["project_override"], job.Account_Name)
    award_type_name = case_insensitive_config_find(config["award_type"], account_info["award_type_name"])
    award_category_name = case_insensitive_config_find(config["award_category"], account_info["award_category"])
    if project_name is not None:
        config_info.update(config["project_override"][project_name])
    elif award_type_name is not None:
        config_info.update(config["award_type"][award_type_name])
    elif award_category_name is not None:
        config_info.update(config["award_category"][award_category_name])
    return config_info


def overburn_active(job, account_info):
    '''Return if overburn should be considered active or not.
    Args:
        job -- pbs.event().job object
        account_info -- account information returned from Ni account_check()

    Returns:
        True if job has an active overburn allocation.  False otherwise.
        Only return true if the project would be out of time otherwise

    Notes:
        Precedence job > award_type > award_category
        All names are case insensitive in the config.

    '''
    overburn_active = False
    if not (account_info["deposits"] == 0 or account_info["balance"] > 0.0):
        # Zero allocation projects can't have an overburn ratio.
        # Can't overburn until you are out of time. Otherwise we need to do this
        overburn_config = get_project_config_info(job, account_info)
        if overburn_config:
            if in_overburn_date_range(overburn_config["overburn_start_month"], overburn_config["overburn_end_month"]):
                account_burn_ratio = (account_info["deposits"] - account_info["balance"]) / account_info["deposits"]
                if (overburn_config["overburn_ratio"] >= account_burn_ratio):
                    overburn_active = True
    return overburn_active


def get_project_priority(job: Any, account_info: Dict[str, Any], config: Dict[str, Any]) -> float:
    '''Get what the project priority for the job should be.
    Priority:
    config default < award_category < award_type < project_override

    '''
    project_priority = config["default_project_priority"]
    project_config_info = get_project_config_info(job, account_info)
    if project_config_info:
        project_priority = project_config_info["project_priority"]
    return float(project_priority)


def set_comm_failure_defaults(job):
    '''In the event of a communication failure and we let the job through anyway
    we still have to set defaults for queue routing.

    '''
    if pbs.event().type != pbs.MOVEJOB:
        job.Resource_List["total_allocation"] = 0.0
        job.Resource_List["current_allocation"] = 0.0
        job.Resource_List["award_category"] = "UNKNOWN"
        job.Resource_List["award_type"] = "UNKNOWN"
        job.Resource_List["burn_ratio"] = 0.0
        job.Resource_List["route_backfill"] = False # Can't determine backfill
        job.Resource_List["overburn"] = False # Can't determine overburn
        job.Resource_List['project_priority'] = config["default_project_priority"]


def success(username, project, job, account_info):
    '''Add relevant data to job (or update data on job if we're moving/altering the job)
    and set up messages indicating that the job passed and is allowed to be queued (at least according to this script).
    Also add in project priority factor so it can be used by the job_sort_formula in the server

    '''
    messages = []
    messages.append(f"Allow Job for user {username} and project {project}")
    if pbs.event().type != pbs.MOVEJOB:
        job.Resource_List["total_allocation"] = account_info["deposits"]
        job.Resource_List["current_allocation"] = account_info["balance"]
        job.Resource_List["award_category"] = account_info["award_category"]
        job.Resource_List["award_type"] = account_info["award_type_name"]
        if abs(float(account_info["deposits"])) < sys.float_info.epsilon:
            job.Resource_List["burn_ratio"] = 0.0
        else:
            job.Resource_List["burn_ratio"] = (account_info["deposits"] - account_info["balance"]) / account_info["deposits"]
        route_backfill = job.Account_Name not in config["ignore_negative_balance"] and account_info["balance"] <= 0
        overburn = overburn_active(job, account_info)
        job.Resource_List["route_backfill"] = route_backfill and not overburn  # Don't backfill overburn jobs
        job.Resource_List["overburn"] = overburn and route_backfill  # only "overburn" if we would otherwise backfill
        job.Resource_List['project_priority'] = get_project_priority(job, account_info, config)
        messages.append(f"Job requested Resource_List now set to {str(job.Resource_List)}")
    return messages


def failure(username, project, account_info):
    messages = []
    messages.append(f'Reject Job for user {username} and project {project}')
    reject_message = f"Request rejected.  Reason: {account_info['message']}"
    return messages, reject_message


def get_job_info(event, attr, attr_key=None):
    '''get information from a job or job_o object as appropriate'''
    val = None
    if attr_key is None:
        val = getattr(event.job, attr)
    else:
        val = getattr(event.job, attr)[attr_key]
    if hasattr(event, "job_o") and val is None:
        if attr_key is None:
            val = getattr(event.job_o, attr)
        else:
            val = getattr(event.job_o, attr)[attr_key]
    return val


def get_right_queue(event, server):
    queue = get_job_info(event, "queue")
    if queue == '':
        queue = server.default_queue
    return queue


def handle_response(account_info, username, event, server):
    pbs.logmsg(pbs.LOG_DEBUG, "Handle Response Entered")
    status = "accept"
    messages = []
    reject_message = None
    queue = get_right_queue(event, server)
    pbs.logmsg(pbs.LOG_DEBUG, f"queue: {queue}, {queue.name}")
    # PBS can apparently give this back as a bool or a string
    allow_negative_allocation = get_queue_params(server, queue, "allow_negative_allocation", default=True)
    if type(allow_negative_allocation) == type(""):
        allow_negative_allocation = allow_negative_allocation.lower() in [True, "true", "1"]
    err_str = ErrorCode.ERROR_STR[account_info["error_code"]]
    project = account_info["project"]
    pbs.logmsg(pbs.LOG_DEBUG, f"allow_negative_allocation: {allow_negative_allocation} {type(allow_negative_allocation)}")
    if err_str == "NONE":
        pbs.logmsg(pbs.LOG_DEBUG, "No Error Code")
        messages = success(username, project, event.job, account_info)
    elif (err_str == "INSUFFICIENT_BALANCE" and
          (event.job.Account_Name in config["ignore_negative_balance"] or allow_negative_allocation)):
        pbs.logmsg(pbs.LOG_DEBUG, "negative allowed")
        messages = success(username, project, event.job, account_info)
    else:
        status = "reject"
        messages, reject_message = failure(username, project, account_info)
    return status, messages, reject_message


def check_account(config):
    '''Make REST query to Ni and check to see if the account is valid.

    '''

    target_events = [pbs.QUEUEJOB, pbs.MOVEJOB, pbs.MODIFYJOB]
    if event.type in target_events:
        project = get_job_info(event, "Account_Name")
        full_username = event.requestor
        end_pos = full_username.find('@')
        username = full_username[:end_pos] if end_pos != -1 else full_username
        queue = get_right_queue(event, server)
        allow_account_check_failure = get_queue_params(server, queue, "allow_account_check_failure", default=True)
        if type(allow_account_check_failure) == type(""):
            allow_account_check_failure = allow_account_check_failure.lower() in [True, "true", "1"]
        pbs.logmsg(pbs.LOG_DEBUG, f"Check allocation for user {username} and project {project}")
        ni_resource = get_queue_params(server, queue, "ni_resource", default=config["default_ni_resource"])
        if ni_resource is None:
            event.reject("resource not configured for account_check.  Please contact support.")
        try:
            account_info = sapi.account_check(project, user=username, resource=ni_resource, fast=True)
        except (Exception, SystemExit) as ex:
            pbs.logmsg(pbs.LOG_ERROR, f"Error contacting accounting system: {str(ex)}")
            pbs.logmsg(pbs.LOG_DEBUG, f"Error information from hook:\n{traceback.format_exc()}")
            if allow_account_check_failure:
                # project will be unset because we don't have a valid one from Ni.
                # Still at least insert the default project priority:
                set_comm_failure_defaults(event.job)
                event.accept()
            event.reject("An error occurred while contacting the accounting system.  Please contact support.")
        else:
            status, messages, reject_message = handle_response(account_info, username, event, server)
            if status == "accept":
                # reset the project to the value in account_info
                # needed for suballocation support.
                if pbs.event().type == pbs.QUEUEJOB and account_info["project"] is not None:
                    event.job.project = account_info["project"]  # This needs to be the project out of Ni.  Handles suballocations.
                for message in messages:
                    pbs.logmsg(pbs.LOG_DEBUG, message)
                event.accept()
            else:
                for message in messages:
                    pbs.logmsg(pbs.EVENT_JOB, message)
                event.reject(reject_message)


try:
    config = get_config(default_config)
    check_account(config)
except pbs.BadAttributeValueError as ex:
    pbs.logmsg(pbs.LOG_ERROR, f"Exception: {ex}\n{str(traceback.format_exc().strip().splitlines())}")
    event.reject(f"Attempted to set a bad attribute value: {ex}")
except pbs.BadResourceValueError as ex:
    pbs.logmsg(pbs.LOG_ERROR, f"Exception: {ex}\n{str(traceback.format_exc().strip().splitlines())}")
    event.reject(f"Attempted to set a bad resource value: {ex}")
except Exception as ex:
    pbs.logmsg(pbs.LOG_ERROR, f"Exception: {ex}\n{str(traceback.format_exc().strip().splitlines())}")
    event.reject("Exception in account_check hook encountered.  Please contact your administrator.")
