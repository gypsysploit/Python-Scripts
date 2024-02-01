#!/home/opc/py36env/bin/python
#################################################################################################################
# OCI - Scheduled Script
# This software is licensed to you under the Universal Permissive License (UPL) 1.0 as shown at https://oss.oracle.com/licenses/upl
# Created/Modified by     : Rajesh R
# Author email   : rajesh.r@dxc.com
#################################################################################################################
# Application Command line parameters
#
#   -t config  - Config file section to use (tenancy profile)
#   -ip        - Use Instance Principals for Authentication
#   -dt        - Use Instance Principals with delegation token for cloud shell
#   -a         - Action - All,Up,Down
#   -tag       - Tag - Default Schedule
#   -rg        - Filter on Region
#   -ic        - include compartment ocid
#   -ec        - exclude compartment ocid
#   -ignrtime  - ignore region time zone
#   -ignormysql- ignore mysql execution
#   -printocid - print ocid of object
#   -topic     - topic to sent summary
#   -log       - send log output to OCI Logging service. Specify the Log OCID
#   -h         - help
#
#################################################################################################################
from logging.handlers import RotatingFileHandler
import oci
import datetime
import calendar
import threading
import time
import sys
import argparse
import os
import Regions
import OCIFunctions

################################################################################################################

"""# Logging the stuff:
import logging
logging.basicConfig(filename='Transcript.log', level=logging.INFO,
                    format='%(name)s:%(asctime)s:%(message)s')"""

# Log rotation

import logging
from logging.handlers import RotatingFileHandler

# Set up the logger
logger = logging.getLogger('')
logger.setLevel(logging.INFO)

# Define the maximum file size (2MB) and the number of backups to keep (3)
max_file_size = 2 * 1024 * 1024  # 2MB
backup_count = 3

# Create a rotating file handler
handler = RotatingFileHandler(
    'Transcript.log', maxBytes=max_file_size, backupCount=backup_count)
handler.setLevel(logging.INFO)

# Define the log format
formatter = logging.Formatter('%(name)s:%(asctime)s:%(message)s')
handler.setFormatter(formatter)

# Add the handler to the logger
logger.addHandler(handler)

# Test logging
logger.info('This is a log message.')

################################################################################################################

logdetails = oci.loggingingestion.models.LogEntryBatch()
logdetails.entries = []

# You can modify / translate the tag names used by this script - case sensitive!!!
AnyDay = "AnyDay"
Weekend = "Weekend"
WeekDay = "WeekDay"
DayOfMonth = "DayOfMonth"
Version = "2022.11.05"

# ============== CONFIGURE THIS SECTION ======================
# OCI Configuration
# ============================================================

ComputeShutdownMethod = "SOFTSTOP"
LogLevel = "ALL"  # Use ALL or ERRORS. When set to ERRORS only a notification will be published if error occurs

AlternativeWeekend = False  # Set to True is your weekend is Friday/Saturday
RateLimitDelay = 2  # Time in seconds to wait before retry of operation

##########################################################################
# Get current host time and utc on execution
##########################################################################
current_host_time = datetime.datetime.today()
current_utc_time = datetime.datetime.utcnow()

##########################################################################
# Print header centered
##########################################################################


def print_header(name):
    chars = int(90)
    MakeLog("")
    MakeLog('#' * chars)
    MakeLog("#" + name.center(chars - 2, " ") + "#")
    MakeLog('#' * chars)


##########################################################################
# Get Current Hour per the region
##########################################################################
def get_current_hour(region, ignore_region_time=False):

    timezdiff = 0  # Default value if no region match is found

    # Find matching time zone for region
    for r in Regions.RegionTime:
        if r[0] == region:
            timezdiff = r[1]

    # Get current host time
    current_time = current_host_time

    # if need to use region time
    if not ignore_region_time:
        current_time = current_utc_time + datetime.timedelta(hours=timezdiff)
        print("Debug: {}".format(current_time))

    # get the variables to return
    iDayOfWeek = current_time.weekday()  # Day of week as a number
    iDay = calendar.day_name[iDayOfWeek]  # Day of week as string
    iCurrentHour = current_time.hour
    iDayOfMonth = current_time.date().day  # Day of the month as a number

    # Get what N-th day of the monday it is. Like 1st, 2nd, 3rd Saturday
    cal = calendar.monthcalendar(current_time.year, current_time.month)
    iDayNr = 0
    daynrcounter = 0
    for week in cal:
        if cal[daynrcounter][iDayOfWeek] == current_time.day:
            if cal[0][iDayOfWeek]:
                iDayNr = daynrcounter + 1
            else:
                iDayNr = daynrcounter

        daynrcounter = daynrcounter + 1

    return iDayOfWeek, iDay, iCurrentHour, iDayOfMonth, iDayNr


##########################################################################
# Configure logging output
##########################################################################
def MakeLog(msg, no_end=False):
    global logdetails
    if no_end:
        print(msg, end="")
    else:
        print(msg)
        logger.info(msg)
        logdetail = oci.loggingingestion.models.LogEntry()
        logdetail.id = datetime.datetime.now(datetime.timezone.utc).isoformat()
        logdetail.data = msg
        logdetail.time = datetime.datetime.now(
            datetime.timezone.utc).isoformat()
        logdetails.entries.append(logdetail)


##########################################################################
# isWeekDay
##########################################################################
def isWeekDay(day):
    weekday = True
    if AlternativeWeekend:
        if day == 4 or day == 5:
            weekday = False
    else:
        if day == 5 or day == 6:
            weekday = False
    return weekday


###############################################
# isDeleted
###############################################
def isDeleted(state):
    deleted = False
    try:
        if state == "TERMINATED" or state == "TERMINATING":
            deleted = True
        if state == "DELETED" or state == "DELETING":
            deleted = True
    except Exception:
        deleted = True
        MakeLog("No lifecyclestate found, ignoring resource")
        MakeLog(state)

    return deleted

##########################################################################
# Load compartments
##########################################################################
def identity_read_compartments(identity, tenancy):

    MakeLog("Loading Compartments...")
    try:
        cs = oci.pagination.list_call_get_all_results(
            identity.list_compartments,
            tenancy.id,
            compartment_id_in_subtree=True,
            retry_strategy=oci.retry.DEFAULT_RETRY_STRATEGY
        ).data

        # Add root compartment which is not part of the list_compartments
        tenant_compartment = oci.identity.models.Compartment()
        tenant_compartment.id = tenancy.id
        tenant_compartment.name = tenancy.name
        tenant_compartment.lifecycle_state = oci.identity.models.Compartment.LIFECYCLE_STATE_ACTIVE
        cs.append(tenant_compartment)

        MakeLog("    Total " + str(len(cs)) + " compartments loaded.")
        return cs

    except Exception as e:
        raise RuntimeError(
            "Error in identity_read_compartments: " + str(e.args))


##########################################################################
# Handle Region
##########################################################################
def autopower_region(region): ## Should this be removed?!

    # Global Paramters for update
    global total_resources
    global ErrorsFound
    global errors
    global success

    MakeLog("Starting script on region {}, executing {} actions".format(
        region, Action))

    threads = []  # Thread array for async AutonomousDB start and rescale
    tcount = 0

    ###############################################
    # Get Current Day, time
    ###############################################
    DayOfWeek, Day, CurrentHour, CurrentDayOfMonth, DayNr = get_current_hour(
        region, cmd.ignore_region_time)

    if AlternativeWeekend:
        MakeLog("Using Alternative weekend (Friday and Saturday as weekend")
    if cmd.ignore_region_time:
        MakeLog("Ignoring Region Datetime, Using local time")

    MakeLog("Day of week: {}, Nth day in Month: {}, IsWeekday: {},  Current hour: {},  Current DayOfMonth: {}".format(
        Day, DayNr, isWeekDay(DayOfWeek), CurrentHour, CurrentDayOfMonth))

    # Investigatin BUG: temporary disabling below logic
    # Array start with 0 so decrease CurrentHour with 1, if hour = 0 then 23
    #CurrentHour = 23 if CurrentHour == 0 else CurrentHour - 1

    ###############################################
    # Find all resources with a Schedule Tag
    ###############################################
    MakeLog("Getting all resources supported by the search function...")
    query = "query {} resources where (definedTags.namespace = '{}')".format(
        ', '.join(supported_resources), PredefinedTag)
    query += " && compartmentId  = '" + compartment_include + \
        "'" if compartment_include else ""
    query += " && compartmentId != '" + compartment_exclude + \
        "'" if compartment_exclude else ""
    sdetails = oci.resource_search.models.StructuredSearchDetails()
    sdetails.query = query

    NoError = True

    try:
        result = oci.pagination.list_call_get_all_results(search.search_resources,
                                                          sdetails,
                                                          **{
                                                              "limit": 1000
                                                          }).data
    except oci.exceptions.ServiceError as response:
        print("Error: {} - {}".format(response.code, response.message))
        result = oci.resource_search.models.ResourceSummaryCollection()
        result.items = []

    #################################################################
    # Find additional resources not found by search (MySQL Service)
    #################################################################
    if not cmd.ignoremysql:

        MakeLog("Finding MySQL instances in {} Compartments...".format(
            len(compartments)))
        for c in compartments:

            # check compartment include and exclude
            if c.lifecycle_state != oci.identity.models.Compartment.LIFECYCLE_STATE_ACTIVE:
                continue
            if compartment_include:
                if c.id != compartment_include:
                    continue
            if compartment_exclude:
                if c.id == compartment_exclude:
                    continue

            mysql_instances = []
            try:
                mysql_instances = oci.pagination.list_call_get_all_results(
                    mysql.list_db_systems,
                    compartment_id=c.id,
                    retry_strategy=oci.retry.DEFAULT_RETRY_STRATEGY
                ).data
            except Exception:
                MakeLog("e", True)
                mysql_instances = []
                continue

            MakeLog(".", True)

            for mysql_instance in mysql_instances:
                if PredefinedTag not in mysql_instance.defined_tags:
                    continue

                summary = oci.resource_search.models.ResourceSummary()
                summary.availability_domain = mysql_instance.availability_domain
                summary.compartment_id = mysql_instance.compartment_id
                summary.defined_tags = mysql_instance.defined_tags
                summary.freeform_tags = mysql_instance.freeform_tags
                summary.identifier = mysql_instance.id
                summary.lifecycle_state = mysql_instance.lifecycle_state
                summary.display_name = mysql_instance.display_name
                summary.resource_type = "MysqlDBInstance"

                try:
                   result.items.append(summary)
                except AttributeError:
                   result.append(summary)

        MakeLog("")

    #################################################################
    # All the items with a schedule are now collected.
    # Let's go thru them and find / validate the correct schedule
    #################################################################

    total_resources += len(result)

    MakeLog("")
    MakeLog("Checking {} Resources...".format(total_resources))

    for resource in result:
        # The search data is not always updated. Get the tags from the actual resource itself, not using the search data.
        resourceOk = False
        if cmd.print_ocid:
            MakeLog("Checking {} ({}) - {}, CurrentState: {}...".format(resource.display_name,
                    resource.resource_type, resource.identifier, resource.lifecycle_state))
        else:
            MakeLog("Checking {} ({}) CurentState: {}...".format(
                resource.display_name, resource.resource_type, resource.lifecycle_state))

        try: # Remove what is not needed before final hand but after testing once again.
            if resource.resource_type == "Instance":
                resourceDetails = compute.get_instance(
                    instance_id=resource.identifier, retry_strategy=oci.retry.DEFAULT_RETRY_STRATEGY).data
                resourceOk = True
            if resource.resource_type == "DbSystem":
                resourceDetails = database.get_db_system(
                    db_system_id=resource.identifier, retry_strategy=oci.retry.DEFAULT_RETRY_STRATEGY).data
                resourceOk = True

            if resource.resource_type == "AutonomousDatabase":
                resourceDetails = database.get_autonomous_database(
                    autonomous_database_id=resource.identifier, retry_strategy=oci.retry.DEFAULT_RETRY_STRATEGY).data
                resourceOk = True

            if resource.resource_type == "MysqlDBInstance":
                resourceDetails = mysql.get_db_system(
                    db_system_id=resource.identifier, retry_strategy=oci.retry.DEFAULT_RETRY_STRATEGY).data
                resourceOk = True

        except:
            MakeLog("Skipping resource, information can not be found")
            resourceOk = False

        if not isDeleted(resource.lifecycle_state) and resourceOk:
            schedule = resourceDetails.defined_tags[PredefinedTag]
            ActiveSchedule = ""

            # Checking the right schedule based on priority
            # from low to high:
            #
            # - Anyday
            # - WeekDay or Weekend
            # - Name of Day (Monday, Tuesday....)
            # - Name of Day, ending with a number to indicate Nth of the month (Saturday1, Saturday2)
            # - Day of month

            if AnyDay in schedule:
                ActiveSchedule = schedule[AnyDay]
            if isWeekDay(DayOfWeek):  # check for weekday / weekend
                if WeekDay in schedule:
                    ActiveSchedule = schedule[WeekDay]
            else:
                if Weekend in schedule:
                    ActiveSchedule = schedule[Weekend]

            if Day in schedule:  # Check for day specific tag (today)
                ActiveSchedule = schedule[Day]

            if "{}{}".format(Day, DayNr) in schedule:  # Check for Nth day of the Month
                ActiveSchedule = schedule["{}{}".format(Day, DayNr)]

            if DayOfMonth in schedule:
                specificDays = schedule[DayOfMonth].split(",")
                for specificDay in specificDays:
                    day, schedulesize = specificDay.split(":")
                    if int(day) == CurrentDayOfMonth:
                        ActiveSchedule = ("{},".format(schedulesize)*24)[:-1]

            #################################################################
            # Check if the active schedule contains exactly 24 numbers for each hour of the day
            #################################################################
            if ActiveSchedule != "":
                try:
                    schedulehours = ActiveSchedule.split("#")[0].split(",")
                    if len(schedulehours) != 24:
                        ErrorsFound = True
                        errors.append(" - Error with schedule of {} - {}, not correct amount of hours, I count {}".format(
                            resource.display_name, ActiveSchedule, len(schedulehours)))
                        MakeLog(" - Error with schedule of {} - {}, not correct amount of hours, i count {}".format(
                            resource.display_name, ActiveSchedule, len(schedulehours)))
                        ActiveSchedule = ""
                except Exception:
                    ErrorsFound = True
                    ActiveSchedule = ""
                    errors.append(
                        " - Error with schedule for {}".format(resource.display_name))
                    MakeLog(
                        " - Error with schedule of {}".format(resource.display_name))
                    MakeLog(sys.exc_info()[0])
            else:
                MakeLog(
                    " - Ignoring instance, as no active schedule for today found")

            ###################################################################################
            # if schedule validated, let see if we can apply the new schedule to the resource
            ###################################################################################

            if ActiveSchedule != "":
                DisplaySchedule = ""
                c = 0
                for h in schedulehours:
                    if c == CurrentHour:
                        DisplaySchedule = DisplaySchedule + "[" + h + "],"
                    else:
                        DisplaySchedule = DisplaySchedule + h + ","
                    c = c + 1

                MakeLog(
                    " - Active schedule for {}: {}".format(resource.display_name, DisplaySchedule))

                if "*" in schedulehours[CurrentHour]:
                    MakeLog(" - Ignoring this service for this hour")

                else:
                    ###################################################################################
                    # Instance: For instances
                    ###################################################################################
                    if resource.resource_type == "Instance":
                        # Check if value is (CPU:Memory) value for flex shapes
                        if schedulehours[CurrentHour][0] == "(" and schedulehours[CurrentHour][-1:] == ")":
                                """ ScheduleHours Comparision"""
                        else:
                            if int(schedulehours[CurrentHour]) == 0 or int(schedulehours[CurrentHour]) == 1:
                                # Only perform action if VM Instance, ignoring any BM instances.
                                if resourceDetails.shape[:2] == "VM":
                                    if resourceDetails.lifecycle_state == "RUNNING" and int(schedulehours[CurrentHour]) == 0:
                                        if Action == "All" or Action == "Down":
                                            MakeLog(
                                                " - Initiate Compute VM shutdown for {}".format(resource.display_name))
                                            Retry = True
                                            while Retry:
                                                try:
                                                    response = compute.instance_action(
                                                        instance_id=resource.identifier, action=ComputeShutdownMethod)
                                                    Retry = False
                                                    success.append(
                                                        " - Initiate Compute VM shutdown for {}".format(resource.display_name))
                                                except oci.exceptions.ServiceError as response:
                                                    if response.status == 429:
                                                        MakeLog("Rate limit kicking in.. waiting {} seconds...".format(
                                                            RateLimitDelay))
                                                        time.sleep(
                                                            RateLimitDelay)
                                                    else:
                                                        ErrorsFound = True
                                                        errors.append(" - Error ({}) Compute VM Shutdown for {} - {}".format(
                                                            response.status, resource.display_name, response.message))
                                                        MakeLog(" - Error ({}) Compute VM Shutdown for {} - {}".format(
                                                            response.status, resource.display_name, response.message))
                                                        Retry = False

                                    if resourceDetails.lifecycle_state == "STOPPED" and int(schedulehours[CurrentHour]) == 1:
                                        if Action == "All" or Action == "Up":
                                            MakeLog(
                                                " - Initiate Compute VM startup for {}".format(resource.display_name))
                                            Retry = True
                                            while Retry:
                                                try:
                                                    response = compute.instance_action(
                                                        instance_id=resource.identifier, action="START")
                                                    Retry = False
                                                    success.append(
                                                        " - Initiate Compute VM startup for {}".format(resource.display_name))
                                                except oci.exceptions.ServiceError as response:
                                                    if response.status == 429:
                                                        MakeLog("Rate limit kicking in.. waiting {} seconds...".format(
                                                            RateLimitDelay))
                                                        time.sleep(
                                                            RateLimitDelay)
                                                    else:
                                                        ErrorsFound = True
                                                        errors.append(" - Error ({}) Compute VM startup for {} - {}".format(
                                                            response.status, resource.display_name, response.message))
                                                        Retry = False

                    ###################################################################################
                    # DBSystem : On/Off operations for Databases VMs
                    ###################################################################################
                    if resource.resource_type == "DbSystem":
                        # Execute On/Off operations for Database VMs
                        if resourceDetails.shape[:2] == "VM":
                            dbnodes = database.list_db_nodes(
                                compartment_id=resource.compartment_id, db_system_id=resource.identifier).data
                            for dbnodedetails in dbnodes:
                                if int(schedulehours[CurrentHour]) == 0 or int(schedulehours[CurrentHour]) == 1:
                                    if dbnodedetails.lifecycle_state == "AVAILABLE" and int(schedulehours[CurrentHour]) == 0:
                                        if Action == "All" or Action == "Down":
                                            MakeLog(
                                                " - Initiate DB VM shutdown for {}".format(resource.display_name))
                                            Retry = True
                                            while Retry:
                                                try:
                                                    response = database.db_node_action(
                                                        db_node_id=dbnodedetails.id, action="STOP")
                                                    Retry = False
                                                    success.append(
                                                        " - Initiate DB VM shutdown for {}".format(resource.display_name))
                                                except oci.exceptions.ServiceError as response:
                                                    if response.status == 429:
                                                        MakeLog("Rate limit kicking in.. waiting {} seconds...".format(
                                                            RateLimitDelay))
                                                        time.sleep(
                                                            RateLimitDelay)
                                                    else:
                                                        ErrorsFound = True
                                                        errors.append(" - Error ({}) DB VM shutdown for {} - {}".format(
                                                            response.status, resource.display_name, response.message))
                                                        Retry = False
                                    if dbnodedetails.lifecycle_state == "STOPPED" and int(schedulehours[CurrentHour]) == 1:
                                        if Action == "All" or Action == "Up":
                                            MakeLog(
                                                " - Initiate DB VM startup for {}".format(resource.display_name))
                                            Retry = True
                                            while Retry:
                                                try:
                                                    response = database.db_node_action(
                                                        db_node_id=dbnodedetails.id, action="START")
                                                    Retry = False
                                                    success.append(
                                                        " - Initiate DB VM startup for {}".format(resource.display_name))
                                                except oci.exceptions.ServiceError as response:
                                                    if response.status == 429:
                                                        MakeLog("Rate limit kicking in.. waiting {} seconds...".format(
                                                            RateLimitDelay))
                                                        time.sleep(
                                                            RateLimitDelay)
                                                    else:
                                                        ErrorsFound = True
                                                        errors.append(" - Error ({}) DB VM startup for {} - {}".format(
                                                            response.status, resource.display_name, response.message))
                                                        Retry = False

                    ###################################################################################
                    # MysqlDBInstance
                    ###################################################################################
                    if resource.resource_type == "MysqlDBInstance":
                        if int(schedulehours[CurrentHour]) == 0 or int(schedulehours[CurrentHour]) == 1:
                            if resourceDetails.lifecycle_state == "ACTIVE" and int(schedulehours[CurrentHour]) == 0:
                                if Action == "All" or Action == "Down":
                                    MakeLog(
                                        " - Initiate MySQL shutdown for {}".format(resource.display_name))
                                    Retry = True
                                    while Retry:
                                        try:
                                            stopaction = oci.mysql.models.StopDbSystemDetails()
                                            stopaction.shutdown_type = "SLOW"
                                            response = mysql.stop_db_system(
                                                db_system_id=resource.identifier, stop_db_system_details=stopaction)
                                            Retry = False
                                            success.append(
                                                " - Initiate MySql shutdown for {}".format(resource.display_name))
                                        except oci.exceptions.ServiceError as response:
                                            if response.status == 429:
                                                MakeLog("Rate limit kicking in.. waiting {} seconds...".format(
                                                    RateLimitDelay))
                                                time.sleep(RateLimitDelay)
                                            else:
                                                ErrorsFound = True
                                                errors.append(" - Error ({}) MySQL Shutdown for {} - {}".format(
                                                    response.status, resource.display_name, response.message))
                                                MakeLog(" - Error ({}) MySQL Shutdown for {} - {}".format(
                                                    response.status, resource.display_name, response.message))
                                                Retry = False

                            if resourceDetails.lifecycle_state == "INACTIVE" and int(schedulehours[CurrentHour]) == 1:
                                if Action == "All" or Action == "Up":
                                    MakeLog(
                                        " - Initiate MySQL startup for {}".format(resource.display_name))
                                    Retry = True
                                    while Retry:
                                        try:
                                            response = mysql.start_db_system(
                                                db_system_id=resource.identifier)
                                            Retry = False
                                            success.append(
                                                " - Initiate MySQL startup for {}".format(resource.display_name))
                                        except oci.exceptions.ServiceError as response:
                                            if response.status == 429:
                                                MakeLog("Rate limit kicking in.. waiting {} seconds...".format(
                                                    RateLimitDelay))
                                                time.sleep(RateLimitDelay)
                                            else:
                                                ErrorsFound = True
                                                errors.append(" - Error ({}) MySQL startup for {} - {}".format(
                                                    response.status, resource.display_name, response.message))
                                                Retry = False

    ###################################################################################
    # Wait for any AutonomousDB and Instance Pool Start and tasks completed
    ###################################################################################
    MakeLog("Waiting for all threads to complete...")
    for t in threads:
        t.join()
    MakeLog("Region {} Completed.".format(region))


##########################################################################
# Main
##########################################################################
# Get Command Line Parser
parser = argparse.ArgumentParser()
parser.add_argument('-t', default="", dest='config_profile',
                    help='Config file section to use (tenancy profile)')
parser.add_argument('-ip', action='store_true', default=False,
                    dest='is_instance_principals', help='Use Instance Principals for Authentication')
parser.add_argument('-dt', action='store_true', default=False,
                    dest='is_delegation_token', help='Use Delegation Token for Authentication')
parser.add_argument('-a', default="All", dest='action',
                    help='Action All, Down, Up')
parser.add_argument('-tag', default="Schedule", dest='tag',
                    help='Tag to examine, Default=Schedule')
parser.add_argument('-rg', default="", dest='filter_region',
                    help='Filter Region')
parser.add_argument('-ic', default="", dest='compartment_include',
                    help='Include Compartment OCID')
parser.add_argument('-ec', default="", dest='compartment_exclude',
                    help='Exclude Compartment OCID')
parser.add_argument('-ignrtime', action='store_true', default=False,
                    dest='ignore_region_time', help='Ignore Region Time - Use Host Time')
parser.add_argument('-ignoremysql', action='store_true', default=False,
                    dest='ignoremysql', help='Ignore MYSQL processing')
parser.add_argument('-printocid', action='store_true', default=False,
                    dest='print_ocid', help='Print OCID for resources')
parser.add_argument('-topic', default="", dest='topic',
                    help='Topic OCID to send summary in home region')
parser.add_argument('-log', default="", dest='log',
                    help='Log OCID to send log output to')

cmd = parser.parse_args()
if cmd.action != "All" and cmd.action != "Down" and cmd.action != "Up":
    parser.print_help()
    sys.exit(0)

####################################
# Assign variables
####################################
filter_region = cmd.filter_region
Action = cmd.action
PredefinedTag = cmd.tag
compartment_exclude = cmd.compartment_exclude if cmd.compartment_exclude else ""
compartment_include = cmd.compartment_include if cmd.compartment_include else ""

####################################
# Start print time info
####################################
start_time = str(datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
print_header("Running Auto Power On/Off Script")

# Identity extract compartments
config, signer = OCIFunctions.create_signer(
    cmd.config_profile, cmd.is_instance_principals, cmd.is_delegation_token)
compartments = []
tenancy = None
tenancy_home_region = ""

try:
    MakeLog("Starts at " + str(datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    MakeLog("\nConnecting to Identity Service...")
    identity = oci.identity.IdentityClient(config, signer=signer)
    tenancy = identity.get_tenancy(config["tenancy"]).data
    regions = identity.list_region_subscriptions(tenancy.id).data

    for reg in regions:
        if reg.is_home_region:
            tenancy_home_region = str(reg.region_name)

    MakeLog("")
    MakeLog("Version       : " + str(Version))
    MakeLog("Command Line  : " + ' '.join(x for x in sys.argv[1:]))
    MakeLog("Tenant Name   : " + str(tenancy.name))
    # Update the Compartment/Prompt for the compartment ID input.
    MakeLog("Tenant Id     : " + tenancy.id)
    MakeLog("Home Region   : " + tenancy_home_region)
    MakeLog("Action        : " + Action)
    MakeLog("Tag           : " + PredefinedTag)

    if cmd.topic:
        MakeLog("Topic         : " + cmd.topic)
    if cmd.filter_region:
        MakeLog("Filter Region : " + cmd.filter_region)

    MakeLog("")
    compartments = identity_read_compartments(identity, tenancy)

except Exception as e:
    raise RuntimeError("\nError connecting to Identity Service - " + str(e))

############################################
# Define Global Variables to store info
############################################
success = []
errors = []
total_resources = 0
ErrorsFound = False
supported_resources = [
    "instance",
    "instancepool",
    "dbsystem",
    "vmcluster",
    "cloudexadatainfrastructure",
    "autonomousdatabase",
    "odainstance",
    "analyticsinstance",
    "integrationinstance",
    "loadbalancer",
    "goldengatedeployment",
    "disworkspace",
    "visualbuilderinstance"
]

############################################
# Loop on all regions
############################################
for region_name in [str(es.region_name) for es in regions]:

    if cmd.filter_region:
        if cmd.filter_region not in region_name:
            continue

    print_header("Region " + region_name)

    # set the region in the config and signer
    config['region'] = region_name
    signer.region = region_name

    ###############################################
    # services - global used by threads as well
    ###############################################
    compute = oci.core.ComputeClient(config, signer=signer)
    database = oci.database.DatabaseClient(config, signer=signer)
    pool = oci.core.ComputeManagementClient(config, signer=signer)
    search = oci.resource_search.ResourceSearchClient(config, signer=signer)
    oda = oci.oda.OdaClient(config, signer=signer)
    analytics = oci.analytics.AnalyticsClient(config, signer=signer)
    integration = oci.integration.IntegrationInstanceClient(
        config, signer=signer)
    loadbalancer = oci.load_balancer.LoadBalancerClient(config, signer=signer)
    mysql = oci.mysql.DbSystemClient(config, signer=signer)
    goldengate = oci.golden_gate.GoldenGateClient(config, signer=signer)
    dataintegration = oci.data_integration.DataIntegrationClient(
        config, signer=signer)
    visualbuilder = oci.visual_builder.VbInstanceClient(config, signer=signer)

    ###############################################
    # Region
    ###############################################
    autopower_region(region_name)

############################################
# Send summary if Topic Specified
############################################
if cmd.topic:

    # set the home region in the config and signer
    config['region'] = tenancy_home_region
    signer.region = tenancy_home_region

    ns = oci.ons.NotificationDataPlaneClient(config, signer=signer)

    if LogLevel == "ALL" or (LogLevel == "ERRORS" and ErrorsFound):
        MakeLog("\nPublishing notification")
        body_message = "Scaling ({}) just completed. Found {} errors across {} scaleable instances (from a total of {} instances). \nError Details: {}\n\nSuccess Details: {}".format(
            Action, len(errors), len(success), total_resources, errors, success)
        Retry = True

        while Retry:
            try:
                ns_response = ns.publish_message(cmd.topic, {
                                                 "title": "Script ran across tenancy: {}".format(tenancy.name), "body": body_message})
                Retry = False
            except oci.exceptions.ServiceError as ns_response:
                if ns_response.status == 429:
                    MakeLog("Rate limit kicking in.. waiting {} seconds...".format(
                        RateLimitDelay))
                    time.sleep(RateLimitDelay)
                else:
                    MakeLog(
                        "Error ({}) publishing notification - {}".format(ns_response.status, ns_response.message))
                    Retry = False

MakeLog("All tasks done, checked {} resources.".format(total_resources))

if cmd.log:
    config['region'] = tenancy_home_region
    signer.region = tenancy_home_region
    logingest = oci.loggingingestion.LoggingClient(config, signer=signer)
    logdetails.source = "Autopower-script"
    logdetails.type = "Autopower-script-output"
    logdetails.subject = "Autopower operations"
    logdetails.defaultlogentrytime = datetime.datetime.now(
        datetime.timezone.utc).isoformat()

    putlogdetails = oci.loggingingestion.models.PutLogsDetails()
    putlogdetails.specversion = "1.0"
    putlogdetails.log_entry_batches = [logdetails]

    result = logingest.put_logs(log_id=cmd.log, put_logs_details=putlogdetails)

### End ###