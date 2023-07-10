'''Prevents user submission of reservations

To set:
create hook no_user_rsub
import hook no_user_rsub x-application/python default no_user_rsub.py
import hook no_user_rsub x-application/config default no_user_rsub.json
set hook no_user_rsub resvsub

'''
import json
import traceback
import grp
from typing import Any, Dict, List, Tuple
import pbs

ALLOW_OPERATOR = "allow_operator"
USERS = "users"
GROUPS = "groups"

default_config = {ALLOW_OPERATOR: True,
                  USERS: [],
                  GROUPS: []
                 }

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
    #TODO: Need to figure out restricting to specific systems.
    managers = parse_acl(server.managers)
    operators = parse_acl(server.operators)
    return user in managers, user in operators

def user_in_group(user: str, groups: List[str]) -> bool:
    '''Check to see if a user is in any of the groups.'''
    user_groups = set([g.gr_name for g in grp.getgrall() if user in g.gr_mem])
    if user_groups & set(groups):
        return True
    return False

def is_user_allowed(user: str, server: Any, permit_manager: bool=True,
                    permit_operator: bool=True, allowed_users: List=None,
                    allowed_groups: List=None) -> bool:
    '''Return a boolean if a user is or not permitted based on appropriate
    manager or operator permissions, or whether the user is in an apporpriate
    UNIX group or explicitly listed.

    Args:
        user: the user name to check
        server: pbs.server object
        permit_manager: [default: True] if True, allow managers to pass check.
        permit_operator: [default: True] if True, allow operator to pass check.
        allowed_users: [default: None] list of users to pass check
        allowed_groups: [default: [None] list of groups.  If a user is in any
            of the listed groups, allow them to pass the check.

    Returns: True if user is permitted, otherwise False.

    '''
    manager, operator = elevated_user(user, server)
    is_manager = permit_manager and manager
    is_operator = permit_operator and operator
    in_users = user in allowed_users
    in_group = user_in_group(user, allowed_groups)
    return is_manager or is_operator or in_users or in_group

try:
    pbs.logmsg(pbs.LOG_DEBUG, "starting rsub hook")
    server = pbs.server()
    event = pbs.event()
    resv = event.resv
    user = event.requestor
    config = get_config(default_config)
    # This was a special set of exceptions in this list from the example in
    # the PBS Hook Guide.
    pbs_internal_user_list = ["PBS_Server", "Scheduler", "pbs_mom", "root"]
    admin_ulist = pbs_internal_user_list + config[USERS]
    if not is_user_allowed(user, server,
                           permit_operator=config[ALLOW_OPERATOR],
                           allowed_users=admin_ulist,
                           allowed_groups=config[GROUPS],):
        event.reject(f"{user} is not authorized to create reservations on this system.")
    else:
        event.accept()
except Exception as ex:
    pbs.logmsg(pbs.LOG_ERROR, f"Exception: {ex}\n{str(traceback.format_exc().strip().splitlines())}")
    event.reject("Exception in no_user_rsub hook encountered.  Please contact your administrator.")
