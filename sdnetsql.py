import json
import csv
import getpass
import argparse
import sys
import pandas as pd
import numpy as np
from tqdm import tqdm  # progress bar
from colorama import init, Fore, Style  # colored screen output
from sshtunnel import SSHTunnelForwarder  # ssh tunnel to jump host
from rest_api_lib import rest_api_lib  # lib to make queries to vManage
from pathlib import Path  # OS-agnostic file handling

# Separate directories for unprocessed source data and results - CSV and HTML
RAW_OUTPUT_DIR = "raw_data/"
REPORT_DIR = "reports/"
HELP_STRING = 'Usage examples:\n' \
              '- Interface State:\n' \
              'python sdnetsql.py -q "select deviceId,vdevice-host-name,ifname,ip-address,port-type,if-admin-status,if-oper-status from interfaces af-type=ipv4" -u usera -c customera --html\n' \
              ' - All active BFD sessions from all devices\n' \
              'python sdnetsql.py -q "select * from bfd_sessions where state = up" -u usera -c customera --html\n' \
              ' - Get OMP sessions state:\n' \
              'python sdnetsql.py -q "select * from omp_peers" -u usera -c customera --html\n' \
              '- Query only specific device:\n' \
              'python sdnetsql.py -q "select vdevice-host-name,ifname,ip-address,port-type,if-admin-status,if-oper-status from interfaces where vdevice-host-name=jc7003edge01 and af-type=ipv4" -u usera -c customera --html'

# max lines for screen output
SCREEN_ROW_COUNT = 30


class CustomParser(argparse.ArgumentParser):
    """
    Overrides default CLI parser's print_help and error methods
    """

    def print_help(self):
        # Print default help from argparse.ArgumentParser class
        super().print_help()
        # print help messages
        print(HELP_STRING)

    def error(self, message):
        print("error: %s\n" % message)
        print("Use --help or -h for help")
        exit(2)


# -------------------------------------------------------------------------------------------

def parse_args(args=sys.argv[1:]):
    """Parse arguments."""
    parser = CustomParser()
    parser._action_groups.pop()
    required = parser.add_argument_group("required arguments")
    optional = parser.add_argument_group("optional arguments")
    # Required arguments
    required.add_argument(
        "-q", "--query", help="Query, see usage examples", type=str, required=True
    )
    required.add_argument(
        "-u",
        "--user",
        help="Username to connect to network devices",
        type=str,
        required=True,
    )
    required.add_argument(
        "-c", "--customer", help="Customer name", type=str, required=True
    )
    # Optional arguments
    optional.add_argument(
        "--no-connect",
        "-nc",
        default=False,
        action="store_true",
        help="Run without connecting to network devices, uses the output previously collected. Impoves query processing speed",
    )
    optional.add_argument(
        "--screen-output",
        default=True,
        required=False,
        action="store_true",
        help="Prints report to screen. CVS reports are always generated",
    )
    optional.add_argument(
        "--screen-lines",
        default=30,
        type=int,
        required=False,
        help="Number of lines to output for each device",
    )
    optional.add_argument(
        "--html-output",
        "-html",
        default=False,
        action="store_true",
        help="Prints report to HTML. CVS reports are always generated",
    )
    return parser.parse_args(args)


# -------------------------------------------------------------------------------------------

def command_analysis(text):
    """
    :param text: SQL string, for example:

    select first_name,last_name from students where id = 5
    select * from students where first_name = "Mike" or "Andrew" and last_name = "Brown"
    select last_name from students where math_score = "90" or "80" and last_name = "Smith" and year = 7 or 8

    :return: Dictionary built from the input string, for example:

        {'conditions': [{'cond_field': 'math_score',
                         'cond_value': ['"90"',
                                        '"80"']},
                        {'cond_field': 'last_name',
                         'cond_value': '"Smith"'},
                        {'cond_field': 'year',
                         'cond_value': ['7',
                                        '8']}],
         'fields': ['*'],
         'source': 'students'}

    Written by Ilya Zyuzin, McKinnon Secondary College, 07K. 2019.
    """
    fields = []
    source = ""
    conditions = []
    conditions_list = []
    result = {}
    command = text.split()

    if command[0] == "select":
        # field analysis
        if "," in command[1]:
            morefields = command[1].split(",")
            for item in morefields:
                fields.append(item)
        else:
            fields.append(command[1])

        # checking whether 'from' exists
        if command[2] == "from":
            # source
            source = command[3]
        else:
            print("Error: 'from' not found!")

        try:
            if command[4] == "where":
                tempcond = " ".join(command[5:])
                # split conditions by keyword 'and'
                condition = tempcond.split("and")
                # loop until everything has been sorted
                for element in condition:
                    condition_dic = {}
                    # split every condition by keyword '='
                    val = element.split("=")
                    condition_dic["cond_field"] = val[0].strip()

                    conditions_list.append(val[0].strip())

                    if "or" in val[1]:
                        # if there is an 'or' in the request
                        tempvalue = ("").join(val[1])
                        values = tempvalue.split("or")
                        condition_dic["cond_value"] = []
                        for value in values:
                            if value != " ":
                                condition_dic["cond_value"].append(value.strip())
                    else:
                        condition_dic["cond_value"] = val[1].strip()

                    conditions.append(condition_dic)
        except:
            pass
    else:
        print("Invalid Format or Command!")

    # if * is in list, return all fields anyway, so ignore all other selected fields
    if "*" in fields:
        fields[0] = "*"
        del fields[1:]
    else:
        # add 'conditions' fields to the list of fields selected
        fields.extend(conditions_list)
        # remove duplicates
        fields_no_duplicates = []
        [
            fields_no_duplicates.append(item)
            for item in fields
            if item not in fields_no_duplicates
        ]
        fields = fields_no_duplicates

    result["fields"] = fields[0:]
    result["source"] = source
    result["conditions"] = conditions[0:]

    return result


# -------------------------------------------------------------------------------------------

def get_file_path(customer, api_mount, file_type):
    """
    Builds file name from input arguments

    :param customer: Customer name
    :param api_mount: vManage API mount point
    :param file_type: report or raw output
    :return: full path with filename
    """

    if file_type == "report":
        file_name = REPORT_DIR + customer + "/" + api_mount.replace("/", "_")
        # Create directory if does not exit
        Path(REPORT_DIR + customer).mkdir(parents=True, exist_ok=True)
    else:
        file_name = RAW_OUTPUT_DIR + customer + "/" + api_mount.replace("/", "_")
        Path(RAW_OUTPUT_DIR + customer).mkdir(parents=True, exist_ok=True)

    return file_name


# -------------------------------------------------------------------------------------------

def print_to_csv_file(headers, content, file_name):
    """
    Prints text to CSV files, also changes command output where necessary, such as Gi -> GigabitEthernet

    :param headers: CSV headers - List
    :param content: CSV text - List of lists
    :param file_name: output file name - string
    :return: None
    """

    try:
        with open(file_name, "w", newline="") as out_csv:
            csvwriter = csv.writer(out_csv, delimiter=",")
            csvwriter.writerow(headers)
            for item in content:
                csvwriter.writerow(item)
        # print("Writing CSV", file_name)
    except Exception as e:
        print("Error while opening file", e)


# -------------------------------------------------------------------------------------------

def process_csv_files(
        join_dataframes, common_column, fields_to_select, filter, file1, file2, result_file
):
    """
    Joins two dataframes.
    Input parameters:
         - common_column
         - two csv files to join
    Writes raw output to a report file
    """

    if join_dataframes:
        pd1 = pd.read_csv(file1)
        pd2 = pd.read_csv(file2)

        if fields_to_select[0] == "*":
            result_pd = pd.merge(
                pd1, pd2, left_on=common_column[0], right_on=common_column[1]
            )
        else:
            result_pd = pd.merge(
                pd1, pd2, left_on=common_column[0], right_on=common_column[1]
            ).filter(fields_to_select)
    else:
        # If "join_dataframes": false   is source_definition.json
        pd1 = pd.read_csv(file1)
        if fields_to_select[0] == "*":
            result_pd = pd1
        else:
            result_pd = pd1.filter(fields_to_select)

    if filter:
        for filter_item in filter:
            try:
                # handle OR clause in SQL - add multiple filters
                condition_sting = ""

                if isinstance(filter_item["cond_value"], list):
                    # if  filter_item["cond_value"]  is list which means there is more that 1 value
                    for cond_value in filter_item["cond_value"]:
                        # concatenate values in a single string using |
                        # https://stackoverflow.com/questions/19169649/using-str-contains-in-pandas-with-dataframes
                        condition_sting = condition_sting + "|" + cond_value
                    # remove leading "|"
                    condition_sting = condition_sting[1:]
                else:
                    # no a list, just a single value - sting
                    condition_sting = filter_item["cond_value"]

                result_pd = result_pd[
                    result_pd[filter_item["cond_field"]]
                        .astype(str)
                        .str.contains(condition_sting, na=False)
                ]
            except:
                # simply ignore any exceptions, not filtered results
                pass

    result_pd.to_csv(result_file, index=False)


# -------------------------------------------------------------------------------------------
def get_vedges_details(customer, api_response_data, device_hostnames):
    """
    Parses JSON response and builds CSV file with vEdge details

    All other devices, such as vBond, vSmart are excluded
    :param customer: string to build correct directory to store CSV files
    :param api_response_data: JSON response
    :param device_hostnames: optional list of hostnames, works as a filter
    :return: list of deviceId of vEdge devices
    """

    # List of devices - for return
    device_ids = []

    # Get CSV Headers for vEdge devices
    csv_headers = []
    found = False

    for element in api_response_data:
        for key, value in element.items():
            if element["device-type"] == "vedge":
                csv_headers.append(key)
                found = True
        if found:
            # found vEdge device, got headers, no need to process other records
            break

    # Get CSV Data for vEdge devices
    csv_data = []
    found = False

    for element in api_response_data:
        csv_row = []
        for key, value in element.items():
            if element["device-type"] == "vedge":
                # Populate list - CSV row for a vEdge device
                csv_row.append(value)
                # Save device ID
                device_id = element["deviceId"]
                found = True
        if found:
            # only add vEdges to the return data
            # if a specific device_hostname requested,
            if device_hostnames:
                if element["host-name"] in device_hostnames:
                    device_ids.append(device_id)
            else:
                device_ids.append(device_id)
            # Add next row to a CSV data
            csv_data.append(csv_row)

    # Dump data
    print_to_csv_file(
        csv_headers, csv_data, get_file_path(customer, "vedges", "raw_output") + ".csv"
    )

    return device_ids


# -------------------------------------------------------------------------------------------

def run_api_query_and_save_to_csv(customer, sdwan_controller, api_query, device_list, no_connect):
    rows_list = []
    skipped_devices = []

    # If Do Not Connect flag is set, do not make API queries
    # The script uses the output .csv files previously collected
    if no_connect:
        try:
            df = pd.read_csv(get_file_path(customer, api_query.split("?")[0], "raw_output") + ".csv")
        except FileNotFoundError:
            # no such file
            print("Could not read CSV file: ", get_file_path(customer, api_query.split("?")[0], "raw_output") + ".csv")
            print("Try to remove no-connect option")
            return 0
        return len(df.index)

    print(">>> Making API request to", api_query)
    # Initialise progress bar
    pbar = tqdm(total=len(device_list), unit="dev")
    pbar.set_description("Processed devices")

    for device in device_list:
        response = json.loads(sdwan_controller.get_request(api_query + device))
        pbar.set_description("Processing %s" % device)
        pbar.update(1)
        try:
            response_data = response["data"]
        except:
            # if no data returned, skip the device
            skipped_devices.append(device)
            continue
        # pprint.pprint(response)
        # ssh_tunnel.stop()
        # exit(0)
        # print("------------------------------", device, "------------------------------")
        # print(json.dumps(response_data, sort_keys=True, indent=4))

        for element in response_data:
            element["deviceId"] = device
            rows_list.append(element)

    # Got lists of lists, convert it to Dataframe
    df = pd.DataFrame(rows_list)
    # replace NaN with empty strings
    df = df.replace(np.nan, "", regex=True)

    # If query contains device_id put this column as first and replace it with default index
    if "deviceId" in api_query and not df.empty:
        # Rearrange columns to device_id comes first
        cols_to_order = ["deviceId"]
        new_columns = cols_to_order + (df.columns.drop(cols_to_order).tolist())
        df = df[new_columns]
        df.set_index("deviceId", inplace=True)

    #  Dump dataframe to CSV, don't include anything after ? in the filename
    df.to_csv(get_file_path(customer, api_query.split("?")[0], "raw_output") + ".csv")

    if len(skipped_devices) > 0:
        print(Fore.RED + "\n>>> Check if these devices and reachable, couldn't get data from: ", skipped_devices)
        print(Style.RESET_ALL)

    return len(df.index)


# -------------------------------------------------------------------------------------------

def save_report_to_html(csv_file, html_file):
    """
    Converts CVS file to HTML, applying CSS

    :param csv_file: input CSV file
    :param html_file: output HTML file, created in the same directory
    :return:
    """
    # reads source CSV, ignore first index column
    dataframe = pd.read_csv(csv_file, index_col=False)

    # convert Dataframe to HTML, apply CSS
    html_string = '<link rel="stylesheet" href="../../html_css/style.css">' + dataframe.to_html(
        index=False, na_rep=" "
    ).replace(
        '<table border="1" class="dataframe">',
        '<table style = "border:1px solid; border-color: white" class="hoverTable">',
    ).replace(
        "<th>", '<th style = "background-color: #5abfdf" align="left">'
    )
    # write result HTML file
    with open(html_file, "w") as f:
        f.write(html_string)
    print("\nHTML Report saved as: " + str(Path(html_file).resolve()))

# -------------------------------------------------------------------------------------------

def stop_ssh_tunnel(ssh_tunnel):
    # Received data, closing ssh tunnel
    if ssh_tunnel.is_active:
        print("Closing SSH tunnel connection...")
        ssh_tunnel.stop()
        print("")

# -------------------------------------------------------------------------------------------

# placeholder for Pytest
def test_case1():
    assert True

def test_case2():
    assert True

def test_case3():
    assert True

# -------------------------------------------------------------------------------------------
def main():
    # init colorama
    init()

    # Check CLI arguments
    options = parse_args()

    # Parse query from CLI input and populate parameters for Dataframe merge
    query_processed = command_analysis(options.query)

    # Analyse query
    source = query_processed["source"]
    if query_processed["conditions"]:
        query_condition = query_processed["conditions"]
    else:
        query_condition = ""
    fields_to_select = query_processed["fields"]

    with open("datasources.json", "r") as f:
        source_definitions = json.load(f)
    with open("customers.json", "r") as f:
        customers_definitions = json.load(f)

    for item in source_definitions:
        if item["data_source"] == source:
            api_query = item["api_mount"]

    # Get customer name from CLI
    customer_name = options.customer

    vmanage_host = ""
    jump_host = ""

    # Get vManage and Jump Host details from customer definitions
    for item in customers_definitions:
        if item["customer"] == customer_name:
            vmanage_host = item["vmanage_ip"]
            jump_host = item["jump_host"]
    if not vmanage_host:
        # No such customer No vManage defined - existing program
        print(
            "No such Customer or vManage. Please specify valid customer name - see customers.json"
        )
        exit(1)

    print("Found vManage Host: ", vmanage_host, " Connecting via jumphost:", jump_host)

    # Add DeviceID field if not already inclided
    if (
            ("deviceId" in api_query)
            and ("*" not in fields_to_select)
            and ("deviceId" not in fields_to_select)
            and (not any("deviceId" in x["cond_field"] for x in query_condition))
    ):
        fields_to_select.insert(0, "deviceId")

    # Ask for password
    password = getpass.getpass("Password: ")

    # jump host is defined for a customer, build ssh tunnel
    if jump_host:
        try:
            ssh_tunnel = SSHTunnelForwarder(
                jump_host,
                ssh_username=options.user,
                ssh_password=password,
                remote_bind_address=(vmanage_host, 443),
            )
            ssh_tunnel.daemon_forward_servers = True

            ssh_tunnel.start()
        except Exception as e:
            print(str(e))
            print("Jump host is defined, but can't connect to it, exiting...")
            exit(1)

        print(
            "SSH tunnel established:", jump_host,
            "Allocated local port:", ssh_tunnel.local_bind_port,
        )  # show assigned local port
        vmanage_host = "127.0.0.1"  # set vmanage host to local tunnel endpoint
    # ssh tunnel has been built

    # Initialise vManage
    try:
        sdwan_controller = rest_api_lib(
            vmanage_host, ssh_tunnel.local_bind_port, options.user, password
        )
    except:
        print(Fore.RED + "Could not connect to vManage, exiting...")
        ssh_tunnel.stop()
        exit(0)

    # Get vEdge device details
    response = json.loads(sdwan_controller.get_request("device"))
    response_data = response["data"]

    # Build device list to query - can be a list from CLI, or all devices if no DeviceId is specified
    device_list = []
    device_hostnames = []
    found_device_id_in_filter = False

    # if deviceId and hostname already specified in filter - 'where' condition, only query this device
    # increases speed of query processing
    # exception is when multiple device are there in when condition - so item["cond_value"] shouldn't be list
    for item in query_condition:
        if "deviceId" in item["cond_field"] and not (isinstance(item["cond_value"], list)):
            # Querying particular devices
            device_list.append(item["cond_value"])
            found_device_id_in_filter = True
        if "host-name" in item["cond_field"] and not (isinstance(item["cond_value"], list)):
            device_hostnames.append(item["cond_value"])

    if not found_device_id_in_filter:
        # Get all vEdges device IDs, save vEdge device details in a CSV file,
        device_list = get_vedges_details(customer_name, response_data, device_hostnames)

    print(Fore.GREEN + "Got", str(len(device_list)), "vEdge devices")
    print(Style.RESET_ALL)

    # Run the query
    dataframe_size = run_api_query_and_save_to_csv(
        customer_name, sdwan_controller, api_query, device_list, options.no_connect
    )

    if dataframe_size == 0:
        print(Fore.RED + "API query returned no data")
        stop_ssh_tunnel(ssh_tunnel)
        exit(0)

    # Received data, don't need ssh tunnel anymore, closing connection
    stop_ssh_tunnel(ssh_tunnel)

    # Process CSV files and generate reports
    process_csv_files(
        False,
        "",
        fields_to_select,
        query_condition,
        get_file_path(customer_name, api_query.split("?")[0], "raw_output") + ".csv",
        "",
        get_file_path(customer_name, api_query.split("?")[0], "report") + ".csv",
    )

    # print result CSV file to screen unless it's set to False is CLI arguments
    if options.screen_output:
        df = pd.read_csv(
            get_file_path(customer_name, api_query.split("?")[0], "report") + ".csv",
            index_col=0,
        )

        # Get number of rows and columns in Dataframe
        count_row = len(df)

        if count_row > SCREEN_ROW_COUNT:
            print(
                "Returned",
                count_row,
                "but printed only first",
                SCREEN_ROW_COUNT,
                ". Check CVS file for full output",
            )
        print("-" * 80)
        if count_row > 0:
            print(df.head(SCREEN_ROW_COUNT))
            print(Fore.GREEN + "Returned", count_row, "record(s)")
        else:
            print(Fore.RED + "Returned 0 record(s)")

        print(Style.RESET_ALL)
        print("-" * 80)

    if options.html_output:
        save_report_to_html(
            get_file_path(customer_name, api_query.split("?")[0], "report") + ".csv",
            get_file_path(customer_name, api_query.split("?")[0], "report") + ".html",
        )

if __name__ == "__main__":
    main()
