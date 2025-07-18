#!/usr/bin/env python
#
# Azure Linux extension
#
# Copyright (c) Microsoft Corporation
# All rights reserved.
# MIT License
# Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
# documentation files (the ""Software""), to deal in the Software without restriction, including without limitation the
# rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit
# persons to whom the Software is furnished to do so, subject to the following conditions:
# The above copyright notice and this permission notice shall be included in all copies or substantial portions of the
# Software.
# THE SOFTWARE IS PROVIDED *AS IS*, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE
# WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR
# COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR
# OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

import platform
import sys
import json
import os
from shutil import copyfile, rmtree
import stat
import grp
import pwd
import filecmp
import metrics_ext_utils.metrics_constants as metrics_constants
import subprocess
import time
import signal
import metrics_ext_utils.metrics_common_utils as metrics_utils

try:
    import urllib.request as urllib # Python 3+
except ImportError:
    import urllib2 as urllib # Python 2

try:
    import urllib.error as urlerror # Python 3+
except ImportError:
    import urllib2 as urlerror # Python 2

try:
    from urllib.parse import urlparse # Python 3+
except ImportError:
    from urlparse import urlparse # Python 2

# Cloud Environments
PublicCloudName     = "azurepubliccloud"
FairfaxCloudName    = "azureusgovernmentcloud"
MooncakeCloudName   = "azurechinacloud"
USNatCloudName      = "usnat" # EX
USSecCloudName      = "ussec" # RX
ArcACloudName       = "azurestackcloud"
DefaultCloudName    = PublicCloudName # Fallback

ARMDomainMap = {
    PublicCloudName:    "management.azure.com",
    FairfaxCloudName:   "management.usgovcloudapi.net",
    MooncakeCloudName:  "management.chinacloudapi.cn",
    USNatCloudName:     "management.azure.eaglex.ic.gov",
    USSecCloudName:     "management.azure.microsoft.scloud",
    ArcACloudName:      "armmanagement.autonomous.cloud.private"
}


def is_running(is_lad):
    """
    This method is used to check if metrics binary is currently running on the system or not.
    In order to check whether it needs to be restarted from the watcher daemon
    """
    if is_lad:
        metrics_bin = metrics_constants.lad_metrics_extension_bin
    else:
        metrics_bin = metrics_constants.ama_metrics_extension_bin

    proc = subprocess.Popen(["ps  aux | grep MetricsExtension | grep -v grep"], stdout=subprocess.PIPE, shell=True)
    output = proc.communicate()[0]
    if metrics_bin in output.decode('utf-8', 'ignore'):
        return True
    else:
        return False


def stop_metrics_service(is_lad):
    """
    Stop the metrics service if VM is using is systemd, otherwise check if the pid_file exists,
    and if the pid belongs to the MetricsExtension process, if yes, then kill the process
    This method is called before remove_metrics_service by the main extension code
    :param is_lad: boolean whether the extension is LAD or not (AMA)
    """

    if is_lad:
        metrics_ext_bin = metrics_constants.lad_metrics_extension_bin
    else:
        metrics_ext_bin = metrics_constants.ama_metrics_extension_bin

    # If the VM has systemd, then we will use that to stop
    if metrics_utils.is_systemd():
        code = 1
        metrics_service_path = get_metrics_extension_service_path(is_lad)
        metrics_service_name = get_metrics_extension_service_name(is_lad)

        if os.path.isfile(metrics_service_path):
            code = os.system("systemctl stop {0}".format(metrics_service_name))            
        else:
            return False, "Metrics Extension service file does not exist. Failed to stop ME service: {0}.service.".format(metrics_service_name)

        if code != 0:
            return False, "Unable to stop Metrics Extension service: {0}. Failed with code {1}".format(metrics_service_name, code)
    else:
        #This VM does not have systemd, So we will use the pid from the last ran metrics process and terminate it
        _, configFolder = get_handler_vars()
        metrics_conf_dir = configFolder + "/metrics_configs/"
        metrics_pid_path = metrics_conf_dir + "metrics_pid.txt"

        if os.path.isfile(metrics_pid_path):
            pid = ""
            with open(metrics_pid_path, "r") as f:
                pid = f.read()
            if pid != "":
                # Check if the process running is indeed MetricsExtension, ignore if the process output doesn't contain MetricsExtension
                proc = subprocess.Popen(["ps -o cmd= {0}".format(pid)], stdout=subprocess.PIPE, shell=True)
                output = proc.communicate()[0]
                if metrics_ext_bin in output.decode('utf-8', 'ignore'):
                    os.kill(int(pid), signal.SIGKILL)
                else:
                    return False, "Found a different process running with PID {0}. Failed to stop MetricsExtension.".format(pid)
            else:
                return False, "No pid found for a currently running Metrics Extension process in {0}. Failed to stop Metrics Extension.".format(metrics_pid_path)
        else:
            return False, "File containing the pid for the running Metrics Extension process at {0} does not exit. Failed to stop Metrics Extension".format(metrics_pid_path)

    return True, "Successfully stopped metrics-extension service"

def remove_metrics_service(is_lad):
    """
    Remove the metrics service if the VM is using systemd as well as the MetricsExtension Binary
    This method is called after stop_metrics_service by the main extension code during Extension uninstall
    :param is_lad: boolean whether the extension is LAD or not (AMA)
    """

    metrics_service_path = get_metrics_extension_service_path(is_lad)

    if os.path.isfile(metrics_service_path):
        code = os.remove(metrics_service_path)

    if is_lad:
        metrics_ext_bin = metrics_constants.lad_metrics_extension_bin
    else:
        metrics_ext_bin = metrics_constants.ama_metrics_extension_bin

    # Checking To see if the files were successfully removed, since os.remove doesn't return an error code
    if os.path.isfile(metrics_ext_bin):
        remove_code = os.remove(metrics_ext_bin)

    return True, "Successfully removed metrics-extensions service and MetricsExtension binary."

def generate_Arc_MSI_token(resource = "https://ingestion.monitor.azure.com/"):
    """
    This method is used to query the Hyrbid metdadata service of Arc to get the MSI Auth token for the VM and write it to the ME config location
    This is called from the main extension code after config setup is complete
    """
    _, configFolder = get_handler_vars()
    me_config_dir = configFolder + "/metrics_configs/"
    me_auth_file_path = me_config_dir + "AuthToken-MSI.json"
    expiry_epoch_time = ""
    log_messages = ""
    retries = 1
    max_retries = 3
    sleep_time = 5

    if not os.path.exists(me_config_dir):
        log_messages += "Metrics extension config directory - {0} does not exist. Failed to generate MSI auth token fo ME.\n".format(me_config_dir)
        return False, expiry_epoch_time, log_messages
    try:
        data = None
        while retries <= max_retries:
            arc_endpoint = metrics_utils.get_arc_endpoint()
            try:
                msiauthurl = arc_endpoint + "/metadata/identity/oauth2/token?api-version=2019-11-01&resource=" + resource
                req = urllib.Request(msiauthurl, headers={'Metadata':'true'})
                res = urllib.urlopen(req)
            except:
                # The above request is expected to fail and add a key to the path
                authkey_dir = "/var/opt/azcmagent/tokens/"
                if not os.path.exists(authkey_dir):
                    log_messages += "Unable to find the auth key file at {0} returned from the arc msi auth request.".format(authkey_dir)
                    return False, expiry_epoch_time, log_messages
                keys_dir = []
                for filename in os.listdir(authkey_dir):
                    keys_dir.append(filename)

                authkey_path = authkey_dir + keys_dir[-1]
                auth = "basic "
                with open(authkey_path, "r") as f:
                    key = f.read()
                auth += key
                req = urllib.Request(msiauthurl, headers={'Metadata':'true', 'authorization':auth})
                res = urllib.urlopen(req)
                data = json.loads(res.read().decode('utf-8', 'ignore'))

            if not data or "access_token" not in data:
                retries += 1
            else:
                break

            log_messages += "Failed to fetch MSI Auth url. Retrying in {2} seconds. Retry Count - {0} out of Mmax Retries - {1}\n".format(retries, max_retries, sleep_time)
            time.sleep(sleep_time)


        if retries > max_retries:
            log_messages += "Unable to generate a valid MSI auth token at {0}.\n".format(me_auth_file_path)
            return False, expiry_epoch_time, log_messages

        with open(me_auth_file_path, "w") as f:
            f.write(json.dumps(data))

        if "expires_on" in data:
            expiry_epoch_time  = data["expires_on"]
        else:
            log_messages += "Error parsing the msi token at {0} for the token expiry time. Failed to generate the correct token\n".format(me_auth_file_path)
            return False, expiry_epoch_time, log_messages

    except Exception as e:
        log_messages += "Failed to get msi auth token. Please check if VM's system assigned Identity is enabled Failed with error {0}\n".format(e)
        return False, expiry_epoch_time, log_messages

    return True, expiry_epoch_time, log_messages


def generate_MSI_token(identifier_name = '', identifier_value = '', is_lad = True):
    """
    This method is used to query the metdadata service to get the MSI Auth token for the VM and write it to the ME config location
    This is called from the main extension code after config setup is complete
    """

    if metrics_utils.is_arc_installed():
        _, _, _, az_environment, _ = get_imds_values(is_lad)
        if az_environment.lower() == ArcACloudName:
            return generate_Arc_MSI_token("https://monitoring.azs")
        return generate_Arc_MSI_token()
    else:
        _, configFolder = get_handler_vars()
        me_config_dir = configFolder + "/metrics_configs/"
        me_auth_file_path = me_config_dir + "AuthToken-MSI.json"
        expiry_epoch_time = ""
        log_messages = ""
        retries = 1
        max_retries = 3
        sleep_time = 5

        if not os.path.exists(me_config_dir):
            log_messages += "Metrics extension config directory - {0} does not exist. Failed to generate MSI auth token for ME.\n".format(me_config_dir)
            return False, expiry_epoch_time, log_messages
        try:
            data = None
            while retries <= max_retries:
                msiauthurl = "http://169.254.169.254/metadata/identity/oauth2/token?api-version=2018-02-01&resource=https://ingestion.monitor.azure.com/"

                if identifier_name and identifier_value:
                    msiauthurl += '&{0}={1}'.format(identifier_name, identifier_value)

                req = urllib.Request(msiauthurl, headers={'Metadata':'true', 'Content-Type':'application/json'})
                res = urllib.urlopen(req)
                data = json.loads(res.read().decode('utf-8', 'ignore'))

                if not data or "access_token" not in data:
                    retries += 1
                else:
                    break

                log_messages += "Failed to fetch MSI Auth url. Retrying in {2} seconds. Retry Count - {0} out of Mmax Retries - {1}\n".format(retries, max_retries, sleep_time)
                time.sleep(sleep_time)


            if retries > max_retries:
                log_messages += "Unable to generate a valid MSI auth token at {0}.\n".format(me_auth_file_path)
                return False, expiry_epoch_time, log_messages

            with open(me_auth_file_path, "w") as f:
                f.write(json.dumps(data))

            if "expires_on" in data:
                expiry_epoch_time  = data["expires_on"]
            else:
                log_messages += "Error parsing the MSI token at {0} for the token expiry time. Failed to generate the correct token\n".format(me_auth_file_path)
                return False, expiry_epoch_time, log_messages

        except Exception as e:
            log_messages += "Failed to get MSI auth token. Please check if the VM's system assigned identity is enabled or the user assigned identity "
            log_messages += "passed in the extension settings exists and is assigned to this VM. Failed with error {0}\n".format(e)
            return False, expiry_epoch_time, log_messages

        return True, expiry_epoch_time, log_messages

def get_ArcA_MSI_token(resource = "https://monitoring.azs"):
    """
    This method is used to query the Hyrbid metdadata service of ArcA to get the MSI Auth token for the VM
    """
    token_string = ""
    log_messages = ""
    retries = 1
    max_retries = 3
    sleep_time = 5

    try:
        data = None
        while retries <= max_retries:
            arc_endpoint = metrics_utils.get_arc_endpoint()
            try:
                msiauthurl = arc_endpoint + "/metadata/identity/oauth2/token?api-version=2019-11-01&resource=" + resource
                req = urllib.Request(msiauthurl, headers={'Metadata':'true'})
                res = urllib.urlopen(req)
            except:
                # The above request is expected to fail and add a key to the path
                authkey_dir = "/var/opt/azcmagent/tokens/"
                if not os.path.exists(authkey_dir):
                    log_messages += "Unable to find the auth key file at {0} returned from the arc msi auth request.".format(authkey_dir)
                    return False, token_string, log_messages
                keys_dir = []
                for filename in os.listdir(authkey_dir):
                    keys_dir.append(filename)

                authkey_path = authkey_dir + keys_dir[-1]
                auth = "basic "
                with open(authkey_path, "r") as f:
                    key = f.read()
                auth += key
                req = urllib.Request(msiauthurl, headers={'Metadata':'true', 'authorization':auth})
                res = urllib.urlopen(req)
                data = json.loads(res.read().decode('utf-8', 'ignore'))

            if not data or "access_token" not in data:
                retries += 1
            else:
                break

            log_messages += "Failed to fetch MSI Auth url. Retrying in {2} seconds. Retry Count - {0} out of Mmax Retries - {1}\n".format(retries, max_retries, sleep_time)
            time.sleep(sleep_time)


        if retries > max_retries:
            log_messages += "Unable to fetch a valid MSI auth token for {0}.\n".format(resource)
            return False, token_string, log_messages

        token_string = data["access_token"]

    except Exception as e:
        log_messages += "Failed to get msi auth token. Please check if VM's system assigned Identity is enabled Failed with error {0}\n".format(e)
        return False, token_string, log_messages

    return True, token_string, log_messages


def setup_me_service(is_lad, configFolder, monitoringAccount, metrics_ext_bin, me_influx_port, managed_identity="sai", HUtilObj=None):
    """
    Setup the metrics service if VM is using systemd
    :param configFolder: Path for the config folder for metrics extension
    :param monitoringAccount: Monitoring Account name that ME will upload data to
    :param metrics_ext_bin: Path for the binary for metrics extension
    :param me_influx_port: Influxdb port that metrics extension will listen on
    """

    me_service_path = get_metrics_extension_service_path(is_lad)
    me_service_template_path = os.getcwd() + "/services/metrics-extension.service"
    daemon_reload_status = 1

    if not os.path.exists(configFolder):
        raise Exception("Metrics extension config directory does not exist. Failed to set up ME service.")

    if os.path.isfile(me_service_template_path):
        copyfile(me_service_template_path, me_service_path)

        if os.path.isfile(me_service_path):
            os.system(r"sed -i 's+%ME_BIN%+{1}+' {0}".format(me_service_path, metrics_ext_bin))
            os.system(r"sed -i 's+%ME_INFLUX_PORT%+{1}+' {0}".format(me_service_path, me_influx_port))
            os.system(r"sed -i 's+%ME_DATA_DIRECTORY%+{1}+' {0}".format(me_service_path, configFolder))
            os.system(r"sed -i 's+%ME_MONITORING_ACCOUNT%+{1}+' {0}".format(me_service_path, monitoringAccount))
            os.system(r"sed -i 's+%ME_MANAGED_IDENTITY%+{1}+' {0}".format(me_service_path, managed_identity))
            daemon_reload_status = os.system("systemctl daemon-reload")
            if daemon_reload_status != 0:
                message = "Unable to reload systemd after ME service file change. Failed to set up ME service. Check system for hardening. Exit code:" + str(daemon_reload_status)
                if HUtilObj is not None:
                    HUtilObj.log(message)
                else:
                    print('Info: {0}'.format(message))

        else:
            raise Exception("Unable to copy Metrics extension service file to {0}. Failed to set up ME service.".format(me_service_path))
    else:
        raise Exception("Metrics extension service template file does not exist at {0}. Failed to set up ME service.".format(me_service_template_path))
    return True


def start_metrics_cmv2():
    """
    Start the metrics service in CMv2 mode
    """

    # Re using the code to grab the config directories and imds values because start will be called from Enable process outside this script
    log_messages = ""

    metrics_ext_bin = metrics_constants.ama_metrics_extension_bin
    if not os.path.isfile(metrics_ext_bin):
        log_messages += "Metrics Extension binary does not exist. Failed to start ME service."
        return False, log_messages

    # If the VM has systemd, then we use that to start/stop
    metrics_service_name = get_metrics_extension_service_name(False)
    if metrics_utils.is_systemd():
        service_restart_status = os.system("systemctl restart {0}".format(metrics_service_name))
        if service_restart_status != 0:
            log_messages += "Unable to start {0} using systemctl. Failed to start ME service. Check system for hardening.".format(metrics_service_name)
            return False, log_messages
        else:
            return True, log_messages

    return False, log_messages


def start_metrics(is_lad, managed_identity="sai"):
    """
    Start the metrics service if VM is using is systemd, otherwise start the binary as a process and store the pid,
    to a file in the MetricsExtension config directory,
    This method is called after config setup is completed by the main extension code
    :param is_lad: boolean whether the extension is LAD or not (AMA)
    """

    # Re using the code to grab the config directories and imds values because start will be called from Enable process outside this script
    log_messages = ""

    if is_lad:
        metrics_ext_bin = metrics_constants.lad_metrics_extension_bin
    else:
        metrics_ext_bin = metrics_constants.ama_metrics_extension_bin
    if not os.path.isfile(metrics_ext_bin):
        log_messages += "Metrics Extension binary does not exist. Failed to start ME service."
        return False, log_messages

    if is_lad:
        me_influx_port = metrics_constants.lad_metrics_extension_udp_port
    else:
        me_influx_port = metrics_constants.ama_metrics_extension_udp_port

    # If the VM has systemd, then we use that to start/stop
    metrics_service_name = get_metrics_extension_service_name(is_lad)
    if metrics_utils.is_systemd():
        service_restart_status = os.system("systemctl restart {0}".format(metrics_service_name))
        if service_restart_status != 0:
            log_messages += "Unable to start {0} using systemctl. Failed to start ME service. Check system for hardening.".format(metrics_service_name)
            return False, log_messages

    #Else start ME as a process and save the pid to a file so that we can terminate it while disabling/uninstalling
    else:
        _, configFolder = get_handler_vars()
        me_config_dir = configFolder + "/metrics_configs/"
        #query imds to get the subscription id
        az_resource_id, subscription_id, location, az_environment, data = get_imds_values(is_lad)

        if is_lad:
            monitoringAccount = "CUSTOMMETRIC_"+ subscription_id
        else:
            monitoringAccount = "CUSTOMMETRIC_"+ subscription_id + "_" + location

        metrics_pid_path = me_config_dir + "metrics_pid.txt"

        # If LAD, use ME startup arguments for LAD, otherwise use ME startup arguments for AMA
        if is_lad:
            binary_exec_command = "{0} -TokenSource MSI -Input influxdb_udp -InfluxDbHost 127.0.0.1 -InfluxDbUdpPort {1} -DataDirectory {2} -LocalControlChannel -MonitoringAccount {3} -LogLevel Error".format(metrics_ext_bin, me_influx_port, me_config_dir, monitoringAccount)
        else:
            binary_exec_command = "{0} -TokenSource AMCS -ManagedIdentity {1} -Input influxdb_udp,otlp_grpc,otlp_grpc_prom -InfluxDbSocketPath /var/run/azuremonitoragent/mdm_influxdb.socket -LogLevel Error".format(metrics_ext_bin, managed_identity)
        
        proc = subprocess.Popen(binary_exec_command.split(" "), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        time.sleep(3) #sleeping for 3 seconds before checking if the process is still running, to give it ample time to relay crash info
        p = proc.poll()

        if p is None: #Process is running successfully
            metrics_pid = proc.pid

            #write this pid to a file for future use
            with open(metrics_pid_path, "w+") as f:
                f.write(str(metrics_pid))
        else:
            out, err = proc.communicate()
            log_messages += "Unable to run MetricsExtension binary as a process due to error - {0}. Failed to start MetricsExtension.".format(err)
            return False, log_messages
    return True, log_messages


def create_metrics_extension_conf(az_resource_id, aad_url):
    """
    Create the metrics extension config
    :param az_resource_id: Azure Resource ID for the VM
    :param aad_url: AAD auth url for the VM
    """
    conf_json = '''{
  "timeToTerminateInMs": 4000,
  "configurationExpirationPeriodInMinutes": 1440,
  "configurationQueriesFrequencyInSec": 900,
  "configurationQueriesTimeoutInSec": 30,
  "maxAcceptedMetricAgeInSec": 1200,
  "maxDataEtwDelayInSec": 3,
  "maxPublicationAttemptsPerMinute": 5,
  "maxPublicationBytesPerMinute": 10000000,
  "maxPublicationMetricsPerMinute": 500000,
  "maxPublicationPackageSizeInBytes": 2500000,
  "maxRandomPublicationDelayInSec": 25,
  "metricsSerializationVersion": 4,
  "minGapBetweenPublicationAttemptsInSec": 5,
  "publicationTimeoutInSec": 30,
  "staleMonitoringAccountsPeriodInMinutes": 20,
  "internalMetricPublicationTimeoutInMinutes": 20,
  "dnsResolutionPeriodInSec": 180,
  "maxAggregationQueueSize": 500000,
  "initialAccountConfigurationLoadWaitPeriodInSec": 20,
  "etwMinBuffersPerCore": 2,
  "etwMaxBuffersPerCore": 16,
  "etwBufferSizeInKb": 1024,
  "internalQueueSizeManagementPeriodInSec": 900,
  "etwLateHeartbeatAllowedCycleCount": 24,
  "etwSampleRatio": 0,
  "maxAcceptedMetricFutureAgeInSec": 1200,
  "aggregatedMetricDiagnosticTracePeriod": 900,
  "aggregatedMetricDiagnosticTraceMaxSize": 100,
  "enableMetricMetadataPublication": true,
  "enableDimensionTrimming": true,
  "shutdownRequestedThreshold": 5,
  "internalMetricProductionLevel": 0,
  "maxPublicationWithoutResponseTimeoutInSec": 300,
  "maxConfigQueryWithoutResponseTimeoutInSec": 300,
  "maxThumbprintsPerAccountToLoad": 100,
  "maxPacketsToCaptureLocally": 0,
  "maxNumberOfRawEventsPerCycle": 1000000,
  "publicationSimulated": false,
  "maxAggregationTimeoutPerCycleInSec": 20,
  "maxRawEventInputQueueSize": 2000000,
  "publicationIntervalInSec": 60,
  "interningSwapPeriodInMin": 240,
  "interningClearPeriodInMin": 5,
  "enableParallelization": true,
  "enableDimensionSortingOnIngestion": true,
  "rawEtwEventProcessingParallelizationFactor": 1,
  "maxRandomConfigurationLoadingDelayInSec": 120,
  "aggregationProcessingParallelizationFactor": 1,
  "aggregationProcessingPerPartitionPeriodInSec": 20,
  "aggregationProcessingParallelizationVolumeThreshold": 500000,
  "useSharedHttpClients": true,
  "loadFromConfigurationCache": true,
  "restartByDateTimeUtc": "0001-01-01T00:00:00",
  "restartStableIdTarget": "",
  "enableIpV6": false,
  "disableCustomMetricAgeSupport": false,
  "globalPublicationCertificateThumbprint": "",
  "maxHllSerializationVersion": 2,
  "enableNodeOwnerMode": false,
  "performAdditionalAzureHostIpV6Checks": false,
  "compressMetricData": false,
  "publishMinMaxByDefault": true,
  "azureResourceId": "'''+ az_resource_id +'''",
  "aadAuthority": "'''+ aad_url +'''",
  "aadTokenEnvVariable": "MSIAuthToken"
} '''
    return conf_json

def create_custom_metrics_conf(mds_gig_endpoint_region, gig_endpoint = ""):
    """
    Create the metrics extension config
    :param mds_gig_endpoint_region: mds gig endpoint region for the VM
    """
    # Note : mds gig endpoint url is only for 3rd party customers. 1st party endpoint is different

    if not gig_endpoint:
        gig_hostname = mds_gig_endpoint_region + ".monitoring.azure.com"
        gig_ingestion_endpoint = "https://" + gig_hostname + "/api/v1/ingestion/ingest"
    else:
        gig_hostname = urlparse(gig_endpoint).netloc
        gig_ingestion_endpoint = gig_endpoint + "/api/v1/ingestion/ingest"

    conf_json = '''{
        "version": 17,
        "maxMetricAgeInSeconds": 0,
        "endpointsForClientForking": [],
        "homeStampGslbHostname": "''' + gig_hostname + '''",
        "endpointsForClientPublication": [
            "''' + gig_ingestion_endpoint + '''"
        ]
    } '''
    return conf_json

def get_handler_vars():
    """
    This method is taken from the Waagent code. This is used to grab the log and config file location from the json public setting for the Extension
    """

    logFolder = ""
    configFolder = ""
    handler_env_path = os.path.abspath(os.path.join(os.path.dirname( __file__ ), '..', 'HandlerEnvironment.json'))
    if os.path.exists(handler_env_path):
        with open(handler_env_path, 'r') as handler_env_file:
            handler_env_txt = handler_env_file.read()
        handler_env = json.loads(handler_env_txt)
        if type(handler_env) == list:
            handler_env = handler_env[0]
        if "handlerEnvironment" in handler_env:
            if "logFolder" in handler_env["handlerEnvironment"]:
                logFolder = handler_env["handlerEnvironment"]["logFolder"]
            if "configFolder" in handler_env["handlerEnvironment"]:
                configFolder = handler_env["handlerEnvironment"]["configFolder"]

    return logFolder, configFolder


def get_imds_values(is_lad, HUtilObj=None):
    """
    Query imds to get required values for MetricsExtension config for this VM
    """
    retries = 1
    max_retries = 3
    sleep_time = 5
    imds_url = ""
    is_arc = False

    if is_lad:
        imds_url = "http://169.254.169.254/metadata/instance?api-version=2019-03-11"
    else:
        if metrics_utils.is_arc_installed():
            imds_url = metrics_utils.get_arc_endpoint()
            imds_url += "/metadata/instance?api-version=2019-11-01"
            is_arc = True
        else:
            imds_url = "http://169.254.169.254/metadata/instance?api-version=2019-03-11"

    message = "IMDS url to query: " + imds_url
    if HUtilObj is not None:
        HUtilObj.log(message)
    else:
        print('Info: {0}'.format(message))

    data = None
    while retries <= max_retries:

        try:
            req = urllib.Request(imds_url, headers={'Metadata':'true'})
            res = urllib.urlopen(req)
            data = json.loads(res.read().decode('utf-8', 'ignore'))
        except:
            pass

        if "compute" not in data:
            retries += 1
        else:
            break

        time.sleep(sleep_time)

    if retries > max_retries:
        raise Exception("Unable to find 'compute' key in imds query response. Reached max retry limit of - {0} times. Failed to set up ME.".format(max_retries))

    if "resourceId" not in data["compute"]:
        raise Exception("Unable to find 'resourceId' key in imds query response. Failed to set up ME.")

    az_resource_id = data["compute"]["resourceId"]

    if "subscriptionId" not in data["compute"]:
        raise Exception("Unable to find 'subscriptionId' key in imds query response. Failed to set up ME.")

    subscription_id = data["compute"]["subscriptionId"]

    if "location" not in data["compute"]:
        raise Exception("Unable to find 'location' key in imds query response. Failed to set up ME.")

    location = data["compute"]["location"]

    if "azEnvironment" not in data["compute"]:
        raise Exception("Unable to find 'azEnvironment' key in imds query response. Failed to set up ME.")

    az_environment = data["compute"]["azEnvironment"]

    return az_resource_id, subscription_id, location, az_environment, data

def get_arca_endpoints_from_himds():
    """
    Query himds to get required arca endpoints for MetricsExtension config for this connected machine
    """
    retries = 1
    max_retries = 3
    sleep_time = 5
    imds_url = "http://localhost:40342/metadata/endpoints?api-version=2019-11-01"

    if metrics_utils.is_arc_installed():
        imds_url = metrics_utils.get_arc_endpoint()
        imds_url += "/metadata/endpoints?api-version=2019-11-01"

    data = None
    while retries <= max_retries:

        try:
            req = urllib.Request(imds_url, headers={'Metadata':'true'})
            res = urllib.urlopen(req)
            data = json.loads(res.read().decode('utf-8', 'ignore'))
        except:
            pass

        if "dataplaneEndpoints" not in data or "resourceManager" not in data:
            retries += 1
        else:
            break

        time.sleep(sleep_time)

    if retries > max_retries:
        raise Exception("Unable to find 'dataplaneEndpoints' key in imds query response. Reached max retry limit of - {0} times. Failed to set up ME.".format(max_retries))

    if "arcMonitorControlServiceEndpoint" not in data["dataplaneEndpoints"]:
        raise Exception("Unable to find 'arcMonitorControlServiceEndpoint' key in imds query response. Failed to set up ME.")

    mcs_endpoint = data["dataplaneEndpoints"]["arcMonitorControlServiceEndpoint"]
    arm_endpoint = data["resourceManager"]

    return arm_endpoint, mcs_endpoint

def get_arca_ingestion_endpoint_from_mcs():
    """
    Query himds to get required arca endpoints for MetricsExtension config for this connected machine
    """
    retries = 1
    max_retries = 3
    sleep_time = 5

    _, mcs_endpoint = get_arca_endpoints_from_himds()
    az_resource_id, _, _, _, _ = get_imds_values(False)
    msi_token_fetched, mcs_token, log_messages = get_ArcA_MSI_token()
    if not msi_token_fetched:
        raise Exception("Unable to fetch MCS token, error message: " + log_messages)
    

    mcs_config_query_url = mcs_endpoint + az_resource_id + "/agentConfigurations?platform=linux&includeMeConfig=true&api-version=2022-06-02"

    if not mcs_token.lower().startswith("bearer "):
        mcs_token = "Bearer " + mcs_token

    data = None
    while retries <= max_retries:

        # Query imds to get the required information
        req = urllib.Request(mcs_config_query_url, headers={'Metadata':'true', 'Authorization':mcs_token})
        res = urllib.urlopen(req)
        data = json.loads(res.read().decode('utf-8', 'ignore'))

        if "configurations" not in data:
            retries += 1
        else:
            break

        time.sleep(sleep_time)

    if retries > max_retries:
        raise Exception("Unable to find 'configurations' key in amcs query response. Reached max retry limit of - {0} times. Failed to set up ME.".format(max_retries))

    if "content" not in data["configurations"][0]:
        raise Exception("Unable to find 'content' key in amcs query response. Failed to set up ME.")
    
    if "channels" not in data["configurations"][0]["content"]:
        raise Exception("Unable to find 'channels' key in amcs query response. Failed to set up ME.")
    
    if "endpoint" not in data["configurations"][0]["content"]["channels"][0]:
        raise Exception("Unable to find 'endpoint' key in amcs query response. Failed to set up ME.")

    ingestion_endpoint = data["configurations"][0]["content"]["channels"][0]["endpoint"]

    # try:
    #     gig_hostname = urllib.parse.urlparse(ingestion_endpoint).netloc

    # except Exception as e:
    #     raise Exception("Failed to retrieve ingestion host name with Exception='{0}'. ".format(e))

    return ingestion_endpoint

def get_arm_domain(az_environment):
    """
    Return the ARM domain to use based on the Azure environment
    """

    try:
        if az_environment.lower() == ArcACloudName:
            arm_endpoint, _ = get_arca_endpoints_from_himds()
            arm_endpoint_parsed = urlparse(arm_endpoint)
            domain = arm_endpoint_parsed.netloc
        else:
            domain = ARMDomainMap[az_environment.lower()]

    except KeyError:
        raise Exception("Unknown cloud environment \"{0}\". Failed to set up ME.".format(az_environment))

    return domain


def get_metrics_extension_service_path(is_lad):
    """
    Utility method to get the service path
    """
    if(is_lad):
        if os.path.exists("/lib/systemd/system/"):
            return metrics_constants.lad_metrics_extension_service_path
        elif os.path.exists("/usr/lib/systemd/system/"):
            return metrics_constants.lad_metrics_extension_service_path_usr_lib
        else:
            raise Exception("Systemd unit files do not exist at /lib/systemd/system or /usr/lib/systemd/system/. Failed to setup Metrics Extension service.")
    else:
        if os.path.exists("/etc/systemd/system"):
            return metrics_constants.metrics_extension_service_path_etc
        if os.path.exists("/lib/systemd/system/"):
            return metrics_constants.metrics_extension_service_path
        elif os.path.exists("/usr/lib/systemd/system/"):
            return metrics_constants.metrics_extension_service_path_usr_lib
        else:
            raise Exception("Systemd unit files do not exist at /etc/systemd/system, /lib/systemd/system or /usr/lib/systemd/system/. Failed to setup Metrics Extension service.")


def get_metrics_extension_service_name(is_lad):
    """
    Utility method to get the service name
    """
    if(is_lad):    
        return metrics_constants.lad_metrics_extension_service_name
    else:
        return metrics_constants.metrics_extension_service_name


def setup_me(is_lad, managed_identity="sai", HUtilObj=None, is_local_control_channel=True, user=None, group=None):
    """
    The main method for creating and writing MetricsExtension configuration as well as service setup
    :param is_lad: Boolean value for whether the extension is Lad or not (AMA)
    :param is_local_control_channel: Boolean value for whether MetricsExtension needs to be run in `-LocalControlChannel` mode (CMv1 only)
    :param user: User that would own MetricsExtension process. If not specified, would default to the caller, in this case being root
    :param group: Group that would own MetricsExtension process. If not specified, would default to the caller, in this case being root
    """
    _, config_folder = get_handler_vars()
    me_config_dir = config_folder + "/metrics_configs/"
    create_empty_data_directory(me_config_dir)

    if not is_local_control_channel:
        # CMv2 and related modes
        me_monitoring_account = ""
        if user and group:
            # Removing it as permissions might not match, and ME will create this with the right permissions
            remove_file("/var/run/azuremonitoragent/mdm_influxdb.socket")
            # Create user/group for metrics-extension.service if it is requested
            ensure_user_and_group(user, group, True)
            # Append group permissions for folder containing influxDB socket file, so the user can create them - in this case ME
            setup_user_and_group_access("/var/run/azuremonitoragent/", user)
            # In CMv2 with user and group specified, create directory for MetricsExtension config caching
            me_config_dir = "/var/run/azuremetricsext"
            create_empty_data_directory(me_config_dir, user, group)
    else:
        # query imds to get the required information
        az_resource_id, subscription_id, location, az_environment, data = get_imds_values(is_lad)
        arm_domain = get_arm_domain(az_environment)

        # get tenantID
        # The url request will fail due to missing authentication header, but we get the auth url from the header of the request fail exception
        aad_auth_url = ""
        arm_url = "https://{0}/subscriptions/{1}?api-version=2014-04-01".format(arm_domain, subscription_id)
        try:
            req = urllib.Request(arm_url, headers={'Content-Type':'application/json'})

            res = urllib.urlopen(req)

        except urlerror.HTTPError as e:
            err_res = e.headers["WWW-Authenticate"]
            for line in err_res.split(","):
                    if "Bearer authorization_uri" in line:
                            data = line.split("=")
                            aad_auth_url = data[1][1:-1] # Removing the quotes from the front and back
                            break

        except Exception as e:
            message = "Failed to retrieve AAD Authentication URL from " + arm_url + " with Exception='{0}'. ".format(e)
            message += "Continuing with metrics setup without AAD auth url."
            if HUtilObj is not None:
                HUtilObj.log(message)
            else:
                print('Info: {0}'.format(message))

        #create metrics conf
        me_conf = create_metrics_extension_conf(az_resource_id, aad_auth_url)

        #create custom metrics conf
        if az_environment.lower() == ArcACloudName:
            ingestion_endpoint = get_arca_ingestion_endpoint_from_mcs()
            custom_conf = create_custom_metrics_conf(location, ingestion_endpoint)
        else:
            custom_conf = create_custom_metrics_conf(location)

        #write configs to disk
        me_conf_path = me_config_dir + "MetricsExtensionV1_Configuration.json"
        with open(me_conf_path, "w") as f:
            f.write(me_conf)

        if is_lad:
            me_monitoring_account = "CUSTOMMETRIC_"+ subscription_id
        else:
            me_monitoring_account = "CUSTOMMETRIC_"+ subscription_id + "_" +location

        custom_conf_path = me_config_dir + me_monitoring_account.lower() +"_MonitoringAccount_Configuration.json"

        with open(custom_conf_path, "w") as f:
            f.write(custom_conf)

    # Copy MetricsExtension Binary to the bin location
    me_bin_local_path = os.getcwd() + "/MetricsExtensionBin/MetricsExtension"
    if is_lad:
        metrics_ext_bin = metrics_constants.lad_metrics_extension_bin
    else:
        metrics_ext_bin = metrics_constants.ama_metrics_extension_bin

    if is_lad:
        lad_bin_path = "/usr/local/lad/bin/"
        # Checking if directory exists before copying ME bin over to /usr/local/lad/bin/
        if not os.path.exists(lad_bin_path):
            os.makedirs(lad_bin_path)

    # Check if previous file exist at the location, compare the two binaries,
    # If the files are not same, remove the older file, and copy the new one
    # If they are the same, then we ignore it and don't copy
    if os.path.isfile(me_bin_local_path):
        if os.path.isfile(metrics_ext_bin):
            if not filecmp.cmp(me_bin_local_path, metrics_ext_bin):
                # Removing the file in case it is already being run in a process,
                # in which case we can get an error "text file busy" while copying
                os.remove(metrics_ext_bin)
                copyfile(me_bin_local_path, metrics_ext_bin)
                os.chmod(metrics_ext_bin, stat.S_IXGRP | stat.S_IRGRP | stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR | stat.S_IXOTH | stat.S_IROTH)

        else:
            # No previous binary exist, simply copy it and make it executable
            copyfile(me_bin_local_path, metrics_ext_bin)
            os.chmod(metrics_ext_bin, stat.S_IXGRP | stat.S_IRGRP | stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR | stat.S_IXOTH | stat.S_IROTH)
    else:
        raise Exception("Unable to copy MetricsExtension Binary, could not find file at the location {0} . Failed to set up ME.".format(me_bin_local_path))

    if is_lad:
        me_influx_port = metrics_constants.lad_metrics_extension_udp_port
    else:
        me_influx_port = metrics_constants.ama_metrics_extension_udp_port

    # setup metrics extension service
    # If the VM has systemd, then we use that to start/stop
    if metrics_utils.is_systemd():
        setup_me_service(is_lad, me_config_dir, me_monitoring_account, metrics_ext_bin, me_influx_port, managed_identity, HUtilObj)

    return True


def ensure_user_and_group(user, group, create_if_missing=False):
    """
    Ensures if the user and group exists, optionally creating them if it does not exist.
    Group is checked, user is checked and then user is added to the group.
    Returns True if all of them are available (or created), else returns False.
    :param user: linux user
    :param group: linux group
    :param create_if_missing: boolean if true, create the requested user and group, where user belongs to the group
    """
    # Check/Create group if missing
    try:
        grp.getgrnam(group)
        print('Group {0} exists.'.format(group))
    except KeyError:
        if create_if_missing:
            try:
                process = subprocess.Popen(['groupadd', group], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                out, err = process.communicate()
                if process.returncode != 0:
                    print('Failed to create group {0}. stderr: {1}'.format(group, err))
                    return False
                print('Group {0} created.'.format(group))
            except Exception as e:
                print('Error while creating group {0}: {1}'.format(group, e))
                return False
        else:
            print('Group {0} does not exist.'.format(group))
            return False

    # Check/Create user if missing
    try:
        pwd.getpwnam(user)
        print('User {0} exists.'.format(user))
    except KeyError:
        if create_if_missing:
            try:
                process = subprocess.Popen([
                    'useradd', '--no-create-home', '--system', '--shell', '/usr/sbin/nologin', user
                ], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                out, err = process.communicate()
                if process.returncode != 0:
                    print('Failed to create user {0}. stderr: {1}'.format(user, err))
                    return False
                print('User {0} created.'.format(user))
            except Exception as e:
                print('Error while creating user {0}: {1}'.format(user, e))
                return False
        else:
            print('User {0} does not exist.'.format(user))
            return False

    # Add user to group
    try:
        process = subprocess.Popen(['usermod', '-aG', group, user], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = process.communicate()
        if process.returncode != 0:
            print('Failed to add user {0} to group {1}. stderr: {2}'.format(user, group, err))
            return False
        print('User {0} added to group {1}.'.format(user, group))
    except Exception as e:
        print('Error while adding user {0} to group {1}: {2}'.format(user, group, e))
        return False

    print('User {0} added to group {1} (or already a member).'.format(user, group))
    return True



def remove_file(file_path):
    """
    Removes existing file.
    Note: This is important as the older MetricsExtension might have created the socket file with root permissions.
    This mechanism can be removed in the future, if the socket file is renamed.
    """
    try:
        os.remove(file_path)
        print('File {0} has been removed.'.format(file_path))
    except Exception as e:
        print('Error while deleting file {0}: {1}.'.format(file_path, e))


def setup_user_and_group_access(directory, user):
    """
    Gets the group that owns the directory and enables write and execution permissions for group users.
    Then the provided user is added to the group so that it is allowed to write and execute in the directory.
    """
    try:
        # Step 1: Get the group owning the directory
        dir_stat = os.stat(directory)
        gid = dir_stat.st_gid
        dir_group_name = grp.getgrgid(gid).gr_name
        print('Directory group: {0}.'.format(dir_group_name))

        # Step 2: Adjust directory permissions to ensure all group users can write and execute (to create socket file)
        mode = stat.S_IMODE(dir_stat.st_mode)
        desired_bits = stat.S_IWGRP | stat.S_IXGRP

        if (mode & desired_bits) != desired_bits:
            new_mode = mode | desired_bits
            os.chmod(directory, new_mode)
            print('Updated directory permissions to add group write+execute: {0} ({1}).'.format(oct(new_mode), directory))
        else:
            print('Directory already has group write+execute permission: {0} ({1}).'.format(oct(mode), directory))

        # Step 3: Ensure user exists and is in the directory's group, else create it
        return ensure_user_and_group(user, dir_group_name, create_if_missing=True)
    except Exception as e:
        print('Error in setup_user_and_group_access: {0}.'.format(e))
        return False


def create_empty_data_directory(me_config_dir, user=None, group=None, mode=0o755):
    '''
    Creates an empty data directory where MetricsExtension can store cached configurations.
    For CMv1, MetricsExtension requires mdsd to provide all configurations on disk.
    For CMv2, MetricsExtension requires an empty data directory where it can cache its configurations.
    '''
    try:
        # Clear older config directory if exists.
        if os.path.exists(me_config_dir):
            rmtree(me_config_dir)
        os.makedirs(me_config_dir, mode=mode)

        if user and group:
            # Get UID and GID from user and group names
            uid = pwd.getpwnam(user).pw_uid
            gid = grp.getgrnam(group).gr_gid

            # Set the ownership
            os.chown(me_config_dir, uid, gid)

        print('Directory {0} created with ownership {1}:{2}.'.format(me_config_dir, user, group))
    except Exception as e:
        print('Failed to create directory: {0}'.format(e))
