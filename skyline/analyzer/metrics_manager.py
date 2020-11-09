from __future__ import division
import logging
from time import time, sleep
from threading import Thread
from multiprocessing import Process
import os
from os import kill, getpid
import traceback
from sys import version_info
import os.path
from ast import literal_eval
from timeit import default_timer as timer

import settings
from skyline_functions import get_redis_conn, get_redis_conn_decoded
from matched_or_regexed_in_list import matched_or_regexed_in_list

skyline_app = 'analyzer'
skyline_app_logger = '%sLog' % skyline_app
logger = logging.getLogger(skyline_app_logger)
skyline_app_logfile = '%s/%s.log' % (settings.LOG_PATH, skyline_app)
skyline_app_loglock = '%s.lock' % skyline_app_logfile
skyline_app_logwait = '%s.wait' % skyline_app_logfile

python_version = int(version_info[0])

this_host = str(os.uname()[1])

try:
    SERVER_METRIC_PATH = '.%s' % settings.SERVER_METRICS_NAME
    if SERVER_METRIC_PATH == '.':
        SERVER_METRIC_PATH = ''
except:
    SERVER_METRIC_PATH = ''

# @modified 20190524 - Feature #2882: Mirage - periodic_check
#                      Branch #3002: docker
# Moved the interpolation of the MIRAGE_PERIODIC_ variables to the top
# of the file out of spawn_process so they can be accessed in run too
# @added 20190408 - Feature #2882: Mirage - periodic_check
# Add Mirage periodic checks so that Mirage is analysing each metric at
# least once per hour.
try:
    # @modified 20200606 - Bug #3572: Apply list to settings imports
    MIRAGE_PERIODIC_CHECK = list(settings.MIRAGE_PERIODIC_CHECK)
except:
    MIRAGE_PERIODIC_CHECK = False
try:
    MIRAGE_PERIODIC_CHECK_INTERVAL = settings.MIRAGE_PERIODIC_CHECK_INTERVAL
except:
    MIRAGE_PERIODIC_CHECK_INTERVAL = 3600
# @added 20200505 - Feature #2882: Mirage - periodic_check
# Surface this once
try:
    # @modified 20200606 - Bug #3572: Apply list to settings imports
    mirage_periodic_check_namespaces = list(settings.MIRAGE_PERIODIC_CHECK_NAMESPACES)
except:
    mirage_periodic_check_namespaces = []
try:
    ANALYZER_ENABLED = settings.ANALYZER_ENABLED
except:
    ANALYZER_ENABLED = True
    logger.info('warning :: ANALYZER_ENABLED is not declared in settings.py, defaults to True')

# @added 20200528 - Feature #3560: External alert config
try:
    EXTERNAL_ALERTS = list(settings.EXTERNAL_ALERTS)
except:
    EXTERNAL_ALERTS = {}
# @added 20200602 - Feature #3560: External alert config
if EXTERNAL_ALERTS:
    from external_alert_configs import get_external_alert_configs

# @added 20200607 - Feature #3566: custom_algorithms
try:
    MIRAGE_ALWAYS_METRICS = list(settings.MIRAGE_ALWAYS_METRICS)
except:
    MIRAGE_ALWAYS_METRICS = []

# @added 20200827 - Feature #3708: FLUX_ZERO_FILL_NAMESPACES
try:
    FLUX_ZERO_FILL_NAMESPACES = settings.FLUX_ZERO_FILL_NAMESPACES
except:
    FLUX_ZERO_FILL_NAMESPACES = []

# @added 20201017 - Feature #3818: ANALYZER_BATCH_PROCESSING_OVERFLOW_ENABLED
# This was implemented to allow a busy analyzer to offload low priority metrics
# to analyzer_batch, unsuccessfully.  It works, but takes loanger and ages
# actually.  Being left in as may be workable with a different logic.
try:
    ANALYZER_BATCH_PROCESSING_OVERFLOW_ENABLED = settings.ANALYZER_BATCH_PROCESSING_OVERFLOW_ENABLED
except:
    ANALYZER_BATCH_PROCESSING_OVERFLOW_ENABLED = False
# Always disable until refactored to work more efficiently if possible
ANALYZER_BATCH_PROCESSING_OVERFLOW_ENABLED = False

# @added 20201030 - Feature #3812: ANALYZER_ANALYZE_LOW_PRIORITY_METRICS
try:
    ANALYZER_ANALYZE_LOW_PRIORITY_METRICS = settings.ANALYZER_ANALYZE_LOW_PRIORITY_METRICS
except:
    ANALYZER_ANALYZE_LOW_PRIORITY_METRICS = True
# @added 20201030 - Feature #3808: ANALYZER_DYNAMICALLY_ANALYZE_LOW_PRIORITY_METRICS
try:
    ANALYZER_DYNAMICALLY_ANALYZE_LOW_PRIORITY_METRICS = settings.ANALYZER_DYNAMICALLY_ANALYZE_LOW_PRIORITY_METRICS
except:
    ANALYZER_DYNAMICALLY_ANALYZE_LOW_PRIORITY_METRICS = False
# @added 20201018 - Feature #3810: ANALYZER_MAD_LOW_PRIORITY_METRICS
try:
    ANALYZER_MAD_LOW_PRIORITY_METRICS = settings.ANALYZER_MAD_LOW_PRIORITY_METRICS
except:
    ANALYZER_MAD_LOW_PRIORITY_METRICS = 0
# @added 20201030 - Feature #3808: ANALYZER_DYNAMICALLY_ANALYZE_LOW_PRIORITY_METRICS
# Set the default ANALYZER_MAD_LOW_PRIORITY_METRICS to 10 if not set and
# ANALYZER_DYNAMICALLY_ANALYZE_LOW_PRIORITY_METRICS is set.
if ANALYZER_DYNAMICALLY_ANALYZE_LOW_PRIORITY_METRICS:
    if not ANALYZER_MAD_LOW_PRIORITY_METRICS:
        ANALYZER_MAD_LOW_PRIORITY_METRICS = 10

# Determine all the settings that place Analyzer in a mode to handle low
# priority metrics differently
ANALYZER_MANAGE_LOW_PRIORITY_METRICS = False
if ANALYZER_BATCH_PROCESSING_OVERFLOW_ENABLED:
    ANALYZER_MANAGE_LOW_PRIORITY_METRICS = True
if not ANALYZER_ANALYZE_LOW_PRIORITY_METRICS:
    ANALYZER_MANAGE_LOW_PRIORITY_METRICS = True
if ANALYZER_DYNAMICALLY_ANALYZE_LOW_PRIORITY_METRICS:
    ANALYZER_MANAGE_LOW_PRIORITY_METRICS = True
if ANALYZER_MAD_LOW_PRIORITY_METRICS:
    ANALYZER_MANAGE_LOW_PRIORITY_METRICS = True
low_priority_metrics_hash_key = 'analyzer.low_priority_metrics.last_analyzed_timestamp'
metrics_last_timestamp_hash_key = 'analyzer.metrics.last_analyzed_timestamp'

skyline_app_graphite_namespace = 'skyline.%s%s' % (skyline_app, SERVER_METRIC_PATH)

full_uniques = '%sunique_metrics' % settings.FULL_NAMESPACE

LOCAL_DEBUG = False


class Metrics_Manager(Thread):
    """
    The Analyzer class which controls the metrics_manager thread and
    spawned processes.

    All of this functionality was previously done in the Analyzer thread itself
    however with 10s of 1000s of metrics, this process can take longer than a
    minute to achieve, which would make Analyzer lag.  All the original commits
    and references from the Analyzer code has been maintained here, although
    the logically order has been changed and the blocks ordered in a different,
    but more appropriate an efficient manner than they were laid out in Analyzer.
    Further some blocks from Analyzer were removed as with the new consolidated
    methods using sets, they were no longer required.

    """

    def __init__(self, parent_pid):
        """
        Initialize the Metrics_Manager
        """
        super(Metrics_Manager, self).__init__()
        self.redis_conn = get_redis_conn(skyline_app)
        self.redis_conn_decoded = get_redis_conn_decoded(skyline_app)
        self.daemon = True
        self.parent_pid = parent_pid
        self.current_pid = getpid()

    def check_if_parent_is_alive(self):
        """
        Self explanatory
        """
        try:
            kill(self.current_pid, 0)
            kill(self.parent_pid, 0)
        except:
            exit(0)

    def metric_management_process(self, i):
        """
        Create and manage the required lists and Redis sets
        """
        spin_start = time()
        logger.info('metrics_manager :: metric_management_process started')

        last_run_timestamp = 0
        try:
            last_run_timestamp = self.redis_conn_decoded.get('analyzer.metrics_manager.last_run_timestamp')
        except:
            logger.error(traceback.format_exc())
            logger.error('error :: metrics_manager :: failed to generate a list from %s Redis set' % full_uniques)
            last_run_timestamp = 0
        if last_run_timestamp:
            logger.info('metrics_manager :: analyzer.metrics_manager.last_run_timestamp Redis key has not expired, not running')
            return

        unique_metrics = []
        try:
            unique_metrics = list(self.redis_conn_decoded.smembers(full_uniques))
        except:
            logger.error(traceback.format_exc())
            logger.error('error :: metrics_manager :: failed to generate a list from %s Redis set' % full_uniques)
            unique_metrics = []

        # Check if this process is unnecessary
        if len(unique_metrics) == 0:
            logger.error('error :: metrics_manager :: there are no metrics in %s Redis set' % full_uniques)
            return

        ####
        # Check whether any alert settings or metrics have been changed, added
        # or removed.  If so do a full refresh.
        ####
        refresh_redis_alert_sets = False

        ####
        # Create a list of base_names from the unique_metrics
        ####

        # @added 20200723 - Feature #3560: External alert config
        # Speed this up only check alerters if not already in the set
        unique_base_names = []
        logger.info('metrics_manager :: creating unique_base_names list')
        for metric_name in unique_metrics:
            # @added 20191014 - Bug #3266: py3 Redis binary objects not strings
            #                   Branch #3262: py3
            if python_version == 3:
                metric_name = str(metric_name)
            # @modified 20200728 - Bug #3652: Handle multiple metrics in base_name conversion
            # base_name = metric_name.replace(settings.FULL_NAMESPACE, '', 1)
            if metric_name.startswith(settings.FULL_NAMESPACE):
                base_name = metric_name.replace(settings.FULL_NAMESPACE, '', 1)
            else:
                base_name = metric_name

            # @added 20200723 - Feature #3560: External alert config
            # Speed this up only check alerters if not already in the set
            # metric_in_smtp_alerters_set = False
            unique_base_names.append(base_name)
        logger.info('metrics_manager :: created unique_base_names list of %s metrics' % str(len(unique_base_names)))

        #####
        # Check whether any internal or external alert settings have been changed
        # if so do a full refresh
        ####

        # @added 20200528 - Feature #3560: External alert config
        external_alerts = {}
        external_from_cache = None
        internal_alerts = {}
        internal_from_cache = None
        all_alerts = list(settings.ALERTS)
        all_from_cache = None
        if EXTERNAL_ALERTS:
            try:
                external_alerts, external_from_cache, internal_alerts, internal_from_cache, all_alerts, all_from_cache = get_external_alert_configs(skyline_app)
            except:
                logger.error(traceback.format_exc())
                logger.error('error :: metrics_manager :: could not determine external alert configs')
            logger.info('metrics_manager :: retrieved %s external_alerts configurations from_cache %s, %s internal_alerts from_cache %s and %s all_alerts from_cache %s' % (
                str(len(external_alerts)), str(external_from_cache),
                str(len(internal_alerts)), str(internal_from_cache),
                str(len(all_alerts)), str(all_from_cache)))
            if LOCAL_DEBUG:
                logger.debug('debug :: metrics_manager :: all_alerts :: %s' % str(all_alerts))
        if not all_alerts:
            logger.error('error :: metrics_manager :: all_alerts is not set, so creating from settings.ALERTS')
            all_alerts = list(settings.ALERTS)

        # If there was a last known alerts configuration compare it to the
        # current known alerts configuration if they are different do a full
        # refresh

        # @added 20201017 - Feature #3788: snab_flux_load_test
        #                   Feature #3560: External alert config
        last_all_alerts_set = None
        try:
            last_all_alerts_data = self.redis_conn_decoded.get('analyzer.last_all_alerts')
            if last_all_alerts_data:
                last_all_alerts = literal_eval(last_all_alerts_data)
                # A normal sorted nor set can be used as the list has dicts in it
                last_all_alerts_set = sorted(last_all_alerts, key=lambda item: item[0])
                logger.info('metrics_manager :: last_all_alerts_set from analyzer.last_all_alerts Redis set has %s items' % str(len(last_all_alerts_set)))
        except:
            logger.error(traceback.format_exc())
            logger.error('error :: metrics_manager :: failed to generate a list from the analyzer.last_all_alerts Redis key')
            last_all_alerts_set = None
        all_alerts_set = None
        if all_alerts:
            try:
                all_alerts_list = [list(row) for row in all_alerts]
                # A normal sorted nor set can be used as the list has dicts in it
                all_alerts_set = sorted(all_alerts_list, key=lambda item: item[0])
                logger.info('metrics_manager :: all_alerts_set from all_alerts has %s items' % str(len(all_alerts_set)))
            except:
                logger.error(traceback.format_exc())
                logger.error('error :: metrics_manager :: failed to create a sorted list from all_alerts object of type %s' % str(type(all_alerts_list)))

            # Set the last known alert configuration to the current configuration
            try:
                self.redis_conn.set('analyzer.last_all_alerts', str(all_alerts_set))
            except:
                logger.error(traceback.format_exc())
                logger.error('error :: metrics_manager :: failed to set analyzer.last_all_alerts Redis key')

        # Compare the last known with the current, if there was a last known
        # configuration, if different do a full refresh
        if last_all_alerts_set:
            if str(all_alerts_set) != str(last_all_alerts_set):
                logger.info('metrics_manager :: alert settings have changed, sets will be refreshed')
                refresh_redis_alert_sets = True

        # Compare the current unique_metrics to the last smtp_alerter_metrics +
        # non_smtp_alerter_metrics, if they have changed do a full refresh
        if not refresh_redis_alert_sets:
            smtp_alerter_metrics = []
            non_smtp_alerter_metrics = []
            try:
                smtp_alerter_metrics = list(self.redis_conn_decoded.smembers('analyzer.smtp_alerter_metrics'))
            except:
                logger.error(traceback.format_exc())
                logger.error('error :: metrics_manager :: failed to get list from analyzer.smtp_alerter_metrics Redis key')
                refresh_redis_alert_sets = True
                smtp_alerter_metrics = None
            try:
                non_smtp_alerter_metrics = list(self.redis_conn_decoded.smembers('analyzer.non_smtp_alerter_metrics'))
            except:
                logger.error(traceback.format_exc())
                logger.error('error :: metrics_manager :: failed to get list from analyzer.non_smtp_alerter_metrics Redis key')
                non_smtp_alerter_metrics = None
            known_alerter_metrics_set = None
            if smtp_alerter_metrics or non_smtp_alerter_metrics:
                try:
                    known_alerter_metrics = smtp_alerter_metrics + non_smtp_alerter_metrics
                    known_alerter_metrics_set = set(known_alerter_metrics)
                except:
                    logger.error(traceback.format_exc())
                    logger.error('error :: metrics_manager :: failed to get list from analyzer.non_smtp_alerter_metrics Redis key')

            # Compare known metrics to current unique_base_names if they are
            # different do a full refresh
            if known_alerter_metrics_set:
                changed_metrics = []
                try:
                    unique_base_names_set = set(list(unique_base_names))
                    if unique_base_names_set == known_alerter_metrics_set:
                        logger.info('metrics_manager :: unique_base_names_set and known_alerter_metrics_set are the same')
                    else:
                        set_difference = unique_base_names_set.difference(known_alerter_metrics_set)
                        for metric in set_difference:
                            changed_metrics.append(metric)
                        logger.info('metrics_manager :: there are %s metrics that have changed, sets will be refreshed' % str(len(changed_metrics)))
                        refresh_redis_alert_sets = True
                        del set_difference
                except:
                    logger.error(traceback.format_exc())
                    logger.error('error :: metrics_manager :: failed to determine hether the unique_base_names_set and known_alerter_metrics_set are different')

        smtp_alerter_metrics = []
        non_smtp_alerter_metrics = []
        mirage_metrics = []

        refresh_redis_alert_sets = True

        # @added 20201104 - Feature #3788: snab_flux_load_test
        #                   Feature #3560: External alert config
        if refresh_redis_alert_sets:
            logger.info('metrics_manager :: sets being refreshed, determining smtp_alerter_metrics')
            all_smtp_alerter_metrics = []
            all_mirage_metrics = []
            mirage_metrics_expiration_times = []
            mirage_metrics_keys = []
            start_refresh = timer()
            for base_name in unique_base_names:
                if base_name not in all_smtp_alerter_metrics:
                    # Use the all_alerts list which includes external alert configs
                    # for alert in settings.ALERTS:
                    for alert in all_alerts:
                        pattern_match = False
                        if str(alert[1]) == 'smtp':
                            try:
                                pattern_match, metric_matched_by = matched_or_regexed_in_list(skyline_app, base_name, [alert[0]])
                                if LOCAL_DEBUG and pattern_match:
                                    logger.debug('debug :: metrics_manager :: %s matched alert - %s' % (base_name, alert[0]))
                                try:
                                    del metric_matched_by
                                except:
                                    pass
                                if pattern_match:
                                    all_smtp_alerter_metrics.append(base_name)
                                    # @added 20160922 - Branch #922: Ionosphere
                                    # Add a Redis set of mirage.unique_metrics
                                    if settings.ENABLE_MIRAGE:
                                        mirage_metric = False
                                        try:
                                            SECOND_ORDER_RESOLUTION_FULL_DURATION = int(alert[3])
                                            if SECOND_ORDER_RESOLUTION_FULL_DURATION > 24:
                                                mirage_metric = True
                                        except:
                                            mirage_metric = False
                                        if mirage_metric:
                                            metric_name = '%s%s' % (settings.FULL_NAMESPACE, base_name)
                                            all_mirage_metrics.append(metric_name)

                                            # @added 20200805 - Task #3662: Change mirage.last_check keys to timestamp value
                                            #                   Feature #3486: analyzer_batch
                                            #                   Feature #3480: batch_processing
                                            # Add the mirage metric and its EXPIRATION_TIME to
                                            # the mirage.metrics_expiration_times so that Mirage
                                            # can determine the metric EXPIRATION_TIME without
                                            # having to create and iterate the all_alerts
                                            # object in the Mirage analysis phase so that the
                                            # reported anomaly timestamp can be used to determine
                                            # whether the EXPIRATION_TIME should be applied to a
                                            # batch metric in the alerting and Ionosphere contexts
                                            # mirage_alert_expiration_data = [base_name, int(alert[2])]
                                            mirage_alert_expiration_data = [base_name, int(alert[2])]
                                            mirage_metrics_expiration_times.append(mirage_alert_expiration_data)

                                            # @added 20200904 - Task #3730: Validate Mirage running multiple processes
                                            # Also always add the mirage.metrics Redis key for the
                                            # metric which contains its hours_to_resolve so
                                            # that the spin_process can add the mirage check
                                            # files immediately, rather than waiting to add
                                            # the mirage checks all in the alerting phase.
                                            # This is done to reduce the time it takes to
                                            # get through the analysis pipeline.
                                            mirage_metrics_keys.append([base_name, int(alert[2]), SECOND_ORDER_RESOLUTION_FULL_DURATION])
                                    break
                            except:
                                pattern_match = False
            end_classify = timer()
            logger.info('metrics_manager :: classifying metrics took %.6f seconds' % (end_classify - start_refresh))

            logger.info('metrics_manager :: %s all_smtp_alerter_metrics were determined' % str(len(all_smtp_alerter_metrics)))
            if all_smtp_alerter_metrics:
                smtp_alerter_metrics = list(set(list(all_smtp_alerter_metrics)))
                logger.info('metrics_manager :: %s unique smtp_alerter_metrics determined' % str(len(smtp_alerter_metrics)))
            # Recreate the Redis set the analyzer.smtp_alerter_metrics
            if smtp_alerter_metrics:
                logger.info('metrics_manager :: recreating the analyzer.smtp_alerter_metrics Redis set')
                try:
                    self.redis_conn.sadd('new_analyzer.smtp_alerter_metrics', *set(smtp_alerter_metrics))
                except:
                    logger.error(traceback.format_exc())
                    logger.error('error :: metrics_manager :: failed to add multiple members to the new_analyzer.smtp_alerter_metrics Redis set')
                try:
                    self.redis_conn.delete('analyzer.smtp_alerter_metrics.old')
                except:
                    pass
                try:
                    self.redis_conn.rename('analyzer.smtp_alerter_metrics', 'analyzer.smtp_alerter_metrics.old')
                except:
                    pass
                try:
                    # @added 20180423 - Feature #2360: CORRELATE_ALERTS_ONLY
                    #                   Branch #2270: luminosity
                    # Add a Redis set of smtp_alerter_metrics for Luminosity to only
                    # cross correlate on metrics with an alert setting
                    self.redis_conn.rename('new_analyzer.smtp_alerter_metrics', 'analyzer.smtp_alerter_metrics')
                except:
                    pass
                try:
                    self.redis_conn.delete('analyzer.smtp_alerter_metrics.old')
                except:
                    pass
                logger.info('metrics_manager :: recreated the analyzer.smtp_alerter_metrics Redis set')

            logger.info('metrics_manager :: determing non_smtp_alerter_metrics')
            try:
                unique_base_names_set = set(list(unique_base_names))
                smtp_alerter_metrics_set = set(list(smtp_alerter_metrics))
                if unique_base_names_set == smtp_alerter_metrics_set:
                    logger.info('metrics_manager :: unique_base_names_set and smtp_alerter_metrics_set are the same, no non_smtp_alerter_metrics')
                else:
                    set_difference = unique_base_names_set.difference(smtp_alerter_metrics_set)
                    for metric in set_difference:
                        non_smtp_alerter_metrics.append(metric)
                    logger.info('metrics_manager :: there are %s non_alerter_metrics' % str(len(non_smtp_alerter_metrics)))
                    del set_difference
            except:
                logger.error(traceback.format_exc())
                logger.error('error :: metrics_manager :: failed to determine non_smtp_alerter_metrics from sets')

            # Recreate the Redis set the analyzer.non_smtp_alerter_metrics
            if non_smtp_alerter_metrics:
                logger.info('metrics_manager :: recreating the analyzer.non_smtp_alerter_metrics Redis set')
                try:
                    self.redis_conn.sadd('new_analyzer.non_smtp_alerter_metrics', *set(non_smtp_alerter_metrics))
                except:
                    logger.error(traceback.format_exc())
                    logger.error('error :: metrics_manager :: failed to add multiple members to the new_analyzer.non_smtp_alerter_metrics Redis set')
                try:
                    self.redis_conn.delete('analyzer.non_smtp_alerter_metrics.old')
                except:
                    pass
                try:
                    self.redis_conn.rename('analyzer.non_smtp_alerter_metrics', 'analyzer.non_smtp_alerter_metrics.old')
                except:
                    pass
                try:
                    self.redis_conn.rename('new_analyzer.non_smtp_alerter_metrics', 'analyzer.non_smtp_alerter_metrics')
                except:
                    pass
                try:
                    self.redis_conn.delete('analyzer.non_smtp_alerter_metrics.old')
                except:
                    pass
                logger.info('metrics_manager :: recreated the analyzer.non_smtp_alerter_metrics Redis set')

            try:
                self.redis_conn.sunionstore('aet.analyzer.smtp_alerter_metrics', 'analyzer.smtp_alerter_metrics')
                logger.info('metrics_manager :: copied Redis set analyzer.smtp_alerter_metrics to aet.analyzer.smtp_alerter_metrics via sunion')
            except:
                logger.error(traceback.format_exc())
                logger.error('error :: failed to copy Redis set analyzer.smtp_alerter_metrics to aet.analyzer.smtp_alerter_metrics via sunion')
            try:
                self.redis_conn.sunionstore('aet.analyzer.non_smtp_alerter_metrics', 'analyzer.non_smtp_alerter_metrics')
                logger.info('metrics_manager :: copied Redis set analyzer.non_smtp_alerter_metrics to aet.analyzer.non_smtp_alerter_metrics via sunion')
            except:
                logger.error(traceback.format_exc())
                logger.error('error :: failed to copy Redis set analyzer.non_smtp_alerter_metrics to aet.analyzer.non_smtp_alerter_metrics via sunion')

            logger.info('metrics_manager :: %s mirage metrics determined' % str(len(all_mirage_metrics)))
            if all_mirage_metrics:
                mirage_metrics = list(set(list(all_mirage_metrics)))
                logger.info('metrics_manager :: %s unique mirage_metrics determined' % str(len(mirage_metrics)))

            # Recreate the Redis set the mirage.unique_metrics
            if mirage_metrics:
                logger.info('metrics_manager :: recreating the mirage.unique_metrics Redis set')
                try:
                    self.redis_conn.sadd('new_mirage.unique_metrics', *set(mirage_metrics))
                except:
                    logger.error(traceback.format_exc())
                    logger.error('error :: failed to add multiple members to the new_mirage.unique_metrics Redis set')
                try:
                    self.redis_conn.delete('mirage.unique_metrics.old')
                except:
                    pass
                try:
                    self.redis_conn.rename('mirage.unique_metrics', 'mirage.unique_metrics.old')
                except:
                    logger.error(traceback.format_exc())
                    logger.error('error :: metrics_manager :: failed to rename Redis set mirage.unique_metrics to mirage.unique_metrics.old')
                try:
                    self.redis_conn.rename('new_mirage.unique_metrics', 'mirage.unique_metrics')
                except:
                    logger.error(traceback.format_exc())
                    logger.error('error :: metrics_manager :: failed to rename Redis set new_mirage.unique_metrics to mirage.unique_metrics')
                try:
                    self.redis_conn.delete('mirage.unique_metrics.old')
                except:
                    pass
                logger.info('metrics_manager :: recreated the mirage.unique_metrics Redis set')

            end_refresh = timer()
            logger.info('metrics_manager :: refresh of smtp_alerter_metrics, non_smtp_alerter_metrics and mirage_metrics took %.6f seconds' % (end_refresh - start_refresh))

            if mirage_metrics_expiration_times:
                logger.info('metrics_manager :: managing mirage.hash_key.metrics_expiration_times Redis hash key')
                updated_keys = 0
                added_keys = 0
                removed_keys = 0
                mirage_metrics_expiration_times_errors = 0
                last_metrics_expiration_times = []
                try:
                    raw_last_metrics_expiration_times = self.redis_conn_decoded.hgetall('mirage.hash_key.metrics_expiration_times')
                    for base_name_bytes in raw_last_metrics_expiration_times:
                        base_name = str(base_name_bytes)
                        expiration_time = int(raw_last_metrics_expiration_times[base_name])
                        last_metrics_expiration_times.append([base_name, expiration_time])
                    logger.info('metrics_manager :: %s entries in mirage.hash_key.metrics_expiration_times Redis hash key' % str(len(last_metrics_expiration_times)))
                except:
                    logger.error(traceback.format_exc())
                    logger.error('error :: metrics_manager :: failed to get Redis hash key mirage.hash_key.metrics_expiration_times')
                    last_metrics_expiration_times = []
                # Add them all if there are none in the hash key
                if not last_metrics_expiration_times:
                    logger.info('metrics_manager :: nothing found in Redis hash key, added all %s metrics from mirage_metrics_expiration_times' % (
                        str(len(mirage_metrics_expiration_times))))
                    error_logged = False
                    for item in mirage_metrics_expiration_times:
                        try:
                            self.redis_conn.hset(
                                'mirage.hash_key.metrics_expiration_times',
                                item[0], int(item[1]))
                            added_keys += 1
                        except:
                            mirage_metrics_expiration_times_errors += 1
                            if not error_logged:
                                logger.error(traceback.format_exc())
                                logger.error('error :: metrics_manager :: failed to add entry in mirage.hash_key.metrics_expiration_times for - %s' % str(item))
                                error_logged = True
                    logger.info('metrics_manager :: added all %s metrics to mirage.hash_key.metrics_expiration_times Redis hash' % (
                        str(len(mirage_metrics_expiration_times))))
                # Determine the base_names in the last_metrics_expiration_times
                last_metrics_expiration_times_metrics = []
                if last_metrics_expiration_times:
                    try:
                        last_metrics_expiration_times_metrics = [item[0] for item in last_metrics_expiration_times]
                    except:
                        logger.error(traceback.format_exc())
                        logger.error('error :: metrics_manager :: failed to generate list of metric names from last_metrics_expiration_times')
                if last_metrics_expiration_times_metrics:
                    logger.info('metrics_manager :: checking entries in mirage.hash_key.metrics_expiration_times Redis hash key are correct')
                    error_logged = False
                    for item in mirage_metrics_expiration_times:
                        try:
                            base_name = item[0]
                            if base_name in last_metrics_expiration_times_metrics:
                                last_expiration_time = int(raw_last_metrics_expiration_times[base_name])
                                if last_expiration_time != int(item[1]):
                                    self.redis_conn.hset(
                                        'mirage.hash_key.metrics_expiration_times',
                                        base_name, int(item[1]))
                                    updated_keys += 1
                            else:
                                self.redis_conn.hset(
                                    'mirage.hash_key.metrics_expiration_times',
                                    base_name, int(item[1]))
                                added_keys += 1
                        except:
                            mirage_metrics_expiration_times_errors += 1
                            if not error_logged:
                                logger.error(traceback.format_exc())
                                logger.error('error :: metrics_manager :: failed to manage entry in mirage.hash_key.metrics_expiration_times for - %s' % str(item))
                                error_logged = True
                    logger.info('metrics_manager :: checked entries in mirage.hash_key.metrics_expiration_times Redis hash key, %s updated, %s added' % (
                        str(updated_keys), str(added_keys)))
                    # Remove any metrics in no longer present
                    present_metrics_expiration_times_metrics = []
                    try:
                        present_metrics_expiration_times_metrics = [item[0] for item in mirage_metrics_expiration_times]
                        logger.info('metrics_manager :: %s current known metrics from mirage_metrics_expiration_times' % str(len(present_metrics_expiration_times_metrics)))
                    except:
                        logger.error(traceback.format_exc())
                        logger.error('error :: metrics_manager :: failed to generate list of metric names from mirage_metrics_expiration_times')
                        present_metrics_expiration_times_metrics = None
                    if present_metrics_expiration_times_metrics:
                        logger.info('metrics_manager :: checking if any entries in mirage.hash_key.metrics_expiration_times Redis hash key need to be removed')
                        error_logged = False
                        for base_name in last_metrics_expiration_times_metrics:
                            try:
                                if base_name not in present_metrics_expiration_times_metrics:
                                    self.redis_conn.hdel(
                                        'mirage.hash_key.metrics_expiration_times',
                                        base_name)
                                    removed_keys += 1
                            except:
                                mirage_metrics_expiration_times_errors += 1
                                if not error_logged:
                                    logger.error(traceback.format_exc())
                                    logger.error('error :: metrics_manager :: failed to remove entry from mirage.hash_key.metrics_expiration_times for - %s' % str(base_name))
                                    error_logged = True
                        logger.info('metrics_manager :: removed %s entries in mirage.hash_key.metrics_expiration_times Redis hash key' % str(removed_keys))
                logger.info('metrics_manager :: managed mirage.hash_key.metrics_expiration_times Redis hash key')

            # @added 20200904 - Task #3730: Validate Mirage running multiple processes
            # Also always add the mirage.metrics Redis key for the
            # metric which contains its hours_to_resolve so
            # that the spin_process can add the mirage check
            # files immediately, rather than waiting to add
            # the mirage checks all in the alerting phase.
            # This is done to reduce the time it takes to
            # get through the analysis pipeline.
            # @modified 20201109 - Feature #3830: metrics_manager
            # Changed to a single mirage.hash_key.metrics_resolutions hash key
            # rather than individual mirage.metrics. Redis keys for each mirage
            # metric
            if mirage_metrics_keys:
                logger.info('metrics_manager :: managing the mirage.hash_key.metrics_resolutions Redis hash key')
                last_metrics_resolutions = {}
                try:
                    raw_last_metrics_resolutions = self.redis_conn_decoded.hgetall('mirage.hash_key.metrics_resolutions')
                    for base_name_bytes in raw_last_metrics_resolutions:
                        base_name = str(base_name_bytes)
                        hours_to_resolve = int(raw_last_metrics_resolutions[base_name])
                        last_metrics_resolutions[base_name] = hours_to_resolve
                    logger.info('metrics_manager :: %s entries in mirage.hash_key.metrics_resolutions Redis hash key' % str(len(last_metrics_resolutions)))
                except:
                    logger.error(traceback.format_exc())
                    logger.error('error :: metrics_manager :: failed to get Redis hash key mirage.hash_key.metrics_resolutions')
                    last_metrics_resolutions = {}
                logger.info('metrics_manager :: there are %s metrics in the mirage.hash_key.metrics_resolutions Redis hash key' % str(len(last_metrics_resolutions)))
                logger.info('metrics_manager :: determining if any metrics need to be removed from the mirage.hash_key.metrics_resolutions Redis hash key, via set difference')
                metrics_to_remove = []
                current_metrics_resolutions = {}
                for item in mirage_metrics_keys:
                    base_name = item[0]
                    current_metrics_resolutions[base_name] = item[2]
                if current_metrics_resolutions:
                    try:
                        last_metrics_resolutions_set = set(list(last_metrics_resolutions))
                        current_metrics_resolutions_set = set(list(current_metrics_resolutions))
                        set_difference = last_metrics_resolutions_set.difference(current_metrics_resolutions_set)
                        metrics_to_remove = set_difference
                    except:
                        logger.error(traceback.format_exc())
                        logger.error('error :: metrics_manager :: failed to determine metrics to remove from mirage.hash_key.metrics_resolutions')
                        metrics_to_remove = []
                logger.info('metrics_manager :: there are %s metrics to remove from the mirage.hash_key.metrics_resolutions Redis hash key' % str(len(metrics_to_remove)))
                if metrics_to_remove:
                    metrics_to_remove_error_logged = False
                    metrics_removed = 0
                    for base_name in metrics_to_remove:
                        try:
                            self.redis_conn.hdel(
                                'mirage.hash_key.metrics_resolutions', base_name)
                            metrics_removed += 1
                        except:
                            if not metrics_to_remove_error_logged:
                                logger.error(traceback.format_exc())
                                logger.error('error :: metrics_manager :: failed to determine metrics to remove from mirage.hash_key.metrics_resolutions')
                                metrics_to_remove_error_logged = True
                    logger.info('metrics_manager :: removed %s metrics from the mirage.hash_key.metrics_resolutions Redis hash key' % str(metrics_removed))
                logger.info('metrics_manager :: determining if there are any new metrics to add to the mirage.hash_key.metrics_resolutions Redis hash key, via set difference')
                metrics_to_add = []
                if current_metrics_resolutions:
                    try:
                        last_metrics_resolutions_set = set(list(last_metrics_resolutions))
                        current_metrics_resolutions_set = set(list(current_metrics_resolutions))
                        set_difference = last_metrics_resolutions_set.difference(last_metrics_resolutions_set)
                        metrics_to_add = set_difference
                    except:
                        logger.error(traceback.format_exc())
                        logger.error('error :: metrics_manager :: failed to determine metrics to remove from mirage.hash_key.metrics_resolutions')
                        metrics_to_add = []
                metrics_added = 0
                logger.info('metrics_manager :: there are %s metrics to add to the mirage.hash_key.metrics_resolutions Redis hash key' % str(len(metrics_to_add)))
                if metrics_to_add:
                    metrics_to_add_error_logged = False
                    for base_name in metrics_to_add:
                        try:
                            hours_to_resolve = current_metrics_resolutions[base_name]
                            self.redis_conn.hset(
                                'mirage.hash_key.metrics_resolutions', base_name,
                                hours_to_resolve)
                            metrics_added += 1
                        except:
                            if not metrics_to_add_error_logged:
                                logger.error(traceback.format_exc())
                                logger.error('error :: metrics_manager :: failed to add %s to mirage.hash_key.metrics_resolutions' % base_name)
                                metrics_to_add_error_logged = True
                    logger.info('metrics_manager :: added %s metrics to the mirage.hash_key.metrics_resolutions Redis hash key' % str(metrics_added))

                # Update any changed metric resolutions, this is a fast iterator
                logger.info('metrics_manager :: checking if any metrics need their resolution updated in the mirage.hash_key.metrics_resolutions Redis hash key')
                metrics_resolutions_updated = 0
                metrics_updated_error_logged = False
                for base_name in current_metrics_resolutions:
                    update_metric_resolution = False
                    try:
                        last_resolution = last_metrics_resolutions[base_name]
                    except:
                        last_resolution = 0
                    try:
                        current_resolution = current_metrics_resolutions[base_name]
                    except:
                        current_resolution = 0
                    if not last_resolution:
                        update_metric_resolution = True
                        last_resolution = current_resolution
                    if last_resolution != current_resolution:
                        update_metric_resolution = True
                    if update_metric_resolution:
                        try:
                            self.redis_conn.hset(
                                'mirage.hash_key.metrics_resolutions', base_name,
                                current_resolution)
                            metrics_resolutions_updated += 1
                        except:
                            if not metrics_updated_error_logged:
                                logger.error(traceback.format_exc())
                                logger.error('error :: metrics_manager :: failed to update the resolution of %s to mirage.hash_key.metrics_resolutions' % base_name)
                                metrics_updated_error_logged = True
                logger.info('metrics_manager :: updated the resolutions of %s metrics in the mirage.hash_key.metrics_resolutions Redis hash key' % str(metrics_resolutions_updated))

        logger.info('metrics_manager :: smtp_alerter_metrics     :: %s' % str(len(smtp_alerter_metrics)))
        logger.info('metrics_manager :: non_smtp_alerter_metrics :: %s' % str(len(non_smtp_alerter_metrics)))
        logger.info('metrics_manager :: mirage_metrics :: %s' % str(len(mirage_metrics)))

        # @added 20200827 - Feature #3708: FLUX_ZERO_FILL_NAMESPACES
        # Analyzer determines what metrics flux should 0 fill by creating
        # the flux.zero_fill_metrics Redis set, which flux references.  This
        # is done in Analyzer because it manages metric Redis sets as it
        # always runs.  It is only managed in Analyzer every 5 mins.
        if FLUX_ZERO_FILL_NAMESPACES:
            manage_flux_zero_fill_namespaces = False
            flux_zero_fill_metrics = []
            # Only manage every 5 mins
            manage_flux_zero_fill_namespaces_redis_key = 'analyzer.manage_flux_zero_fill_namespaces'
            try:
                manage_flux_zero_fill_namespaces = self.redis_conn.get(manage_flux_zero_fill_namespaces_redis_key)
            except Exception as e:
                if LOCAL_DEBUG:
                    logger.error('error :: metrics_manager :: could not query Redis for analyzer.manage_mirage_unique_metrics key: %s' % str(e))
            if not manage_flux_zero_fill_namespaces:
                logger.info('metrics_manager :: managing FLUX_ZERO_FILL_NAMESPACES Redis sets')
                try:
                    self.redis_conn.delete('analyzer.flux_zero_fill_metrics')
                except:
                    logger.error(traceback.format_exc())
                    logger.error('error :: metrics_manager :: failed to delete analyzer.flux_zero_fill_metrics Redis set')
                for i_base_name in unique_base_names:
                    flux_zero_fill_metric = False
                    pattern_match, metric_matched_by = matched_or_regexed_in_list('analyzer', i_base_name, FLUX_ZERO_FILL_NAMESPACES)
                    if pattern_match:
                        flux_zero_fill_metric = True
                    if flux_zero_fill_metric:
                        flux_zero_fill_metrics.append(i_base_name)
                if flux_zero_fill_metrics:
                    logger.info('metrics_manager :: popuating analyzer.flux_zero_fill_metrics Redis set with %s metrics' % str(len(flux_zero_fill_metrics)))
                    try:
                        self.redis_conn.sadd('analyzer.flux_zero_fill_metrics', *set(flux_zero_fill_metrics))
                    except:
                        logger.error(traceback.format_exc())
                        logger.error('error :: metrics_manager :: failed to add multiple members to the analyzer.flux_zero_fill_metrics Redis set')
                try:
                    key_timestamp = int(time())
                    self.redis_conn.setex(manage_flux_zero_fill_namespaces_redis_key, 300, key_timestamp)
                except:
                    logger.error('error :: metrics_manager :: failed to set key :: manage_flux_zero_fill_namespaces_redis_key' % manage_flux_zero_fill_namespaces_redis_key)
                logger.info('metrics_manager :: checking if any metrics need to be removed from analyzer.flux_zero_fill_metrics')
                flux_zero_fill_metrics_to_remove = []
                flux_zero_fill_metrics_list = []
                try:
                    flux_zero_fill_metrics_list = list(self.redis_conn_decoded.smembers('analyzer.flux_zero_fill_metrics'))
                except:
                    logger.info(traceback.format_exc())
                    logger.error('error :: metrics_manager :: failed to generate a list from analyzer.flux_zero_fill_metrics Redis set')
                for flux_zero_fill_base_name in flux_zero_fill_metrics_list:
                    if flux_zero_fill_base_name not in unique_base_names:
                        flux_zero_fill_metrics_to_remove.append(flux_zero_fill_base_name)
                if flux_zero_fill_metrics_to_remove:
                    try:
                        logger.info('metrics_manager :: removing %s metrics from smtp_alerter_metrics' % str(len(flux_zero_fill_metrics_to_remove)))
                        self.redis_conn.srem('analyzer.flux_zero_fill_metrics', *set(flux_zero_fill_metrics_to_remove))
                        # Reload the new set
                        try:
                            flux_zero_fill_metrics_list = list(self.redis_conn_decoded.smembers('analyzer.flux_zero_fill_metrics'))
                        except:
                            logger.info(traceback.format_exc())
                            logger.error('error :: metrics_manager :: failed to generate a list from analyzer.flux_zero_fill_metrics Redis set after removals')
                    except:
                        logger.info(traceback.format_exc())
                        logger.error('error :: metrics_manager :: failed to add multiple members to the analyzer.flux_zero_fill_metrics Redis set')
                else:
                    logger.info('metrics_manager :: no metrics need to remove from analyzer.flux_zero_fill_metrics')
                if flux_zero_fill_metrics_list:
                    # Replace the existing flux.zero_fill_metrics Redis set
                    try:
                        self.redis_conn.sunionstore('flux.zero_fill_metrics', 'analyzer.flux_zero_fill_metrics')
                        logger.info('metrics_manager :: replaced flux.zero_fill_metrics Redis set with the newly created analyzer.flux_zero_fill_metrics set')
                    except:
                        logger.info(traceback.format_exc())
                        logger.error('error :: metrics_manager :: failed to sunionstore flux.zero_fill_metrics from analyzer.flux_zero_fill_metrics Redis sets')
        del unique_base_names

        # @added 20201030 - Feature #3808: ANALYZER_DYNAMICALLY_ANALYZE_LOW_PRIORITY_METRICS
        # Remove any entries in the Redis low_priority_metrics_hash_key
        # that are not in unique_metrics
        if ANALYZER_MANAGE_LOW_PRIORITY_METRICS:
            logger.info('metrics_manager :: managing the Redis hash key %s and removing any metrics not in unique_metrics' % (
                low_priority_metrics_hash_key))
            low_priority_metrics_last_analyzed = []
            raw_low_priority_metrics_last_analyzed = {}
            try:
                raw_low_priority_metrics_last_analyzed = self.redis_conn_decoded.hgetall(low_priority_metrics_hash_key)
                for base_name_bytes in raw_low_priority_metrics_last_analyzed:
                    base_name = str(base_name_bytes)
                    last_analyzed = int(raw_low_priority_metrics_last_analyzed[base_name])
                    low_priority_metrics_last_analyzed.append([base_name, last_analyzed])
            except:
                logger.error(traceback.format_exc())
                logger.error('error :: metrics_manager :: failed to get Redis hash key %s' % (
                    low_priority_metrics_hash_key))
                low_priority_metrics_last_analyzed = []
            del raw_low_priority_metrics_last_analyzed
            low_priority_analyzed_metrics = []
            if low_priority_metrics_last_analyzed:
                try:
                    low_priority_analyzed_metrics = [item[0] for item in low_priority_metrics_last_analyzed]
                    logger.info('metrics_manager :: there are %s metrics in the Redis hash key %s' % (
                        str(len(low_priority_analyzed_metrics)),
                        low_priority_metrics_hash_key))
                except:
                    logger.error(traceback.format_exc())
                    logger.error('error :: metrics_manager :: failed to generate low_priority_metrics_last_analyzed')
                    low_priority_analyzed_metrics = []
                try:
                    del low_priority_metrics_last_analyzed
                except:
                    pass
            if low_priority_analyzed_metrics:
                low_priority_analyzed_metrics_set = None
                try:
                    low_priority_analyzed_metrics_set = set(low_priority_analyzed_metrics)
                except:
                    logger.error(traceback.format_exc())
                    logger.error('error :: metrics_manager :: failed to generate low_priority_analyzed_metrics_set')
                try:
                    del low_priority_analyzed_metrics
                except:
                    pass
                unique_metrics_set = None
                try:
                    unique_metrics_list = list(unique_metrics)
                    unique_metrics_set = set(unique_metrics_list)
                    del unique_metrics_list
                except:
                    logger.error(traceback.format_exc())
                    logger.error('error :: metrics_manager :: failed to generate unique_metrics_set')
                if low_priority_analyzed_metrics_set and unique_metrics_set:
                    low_priority_metrics_to_remove = []
                    try:
                        set_difference = low_priority_analyzed_metrics_set.difference(unique_metrics_set)
                        for metric in set_difference:
                            low_priority_metrics_to_remove.append(metric)
                    except:
                        logger.error(traceback.format_exc())
                        logger.error('error :: metrics_manager :: determining difference between low_priority_analyzed_metrics_set and unique_metrics_set')
                    try:
                        del low_priority_analyzed_metrics_set
                    except:
                        pass
                    try:
                        del unique_metrics_set
                    except:
                        pass
                    try:
                        del set_difference
                    except:
                        pass
                    if low_priority_metrics_to_remove:
                        try:
                            logger.info('metrics_manager :: removing %s metrics from the Redis hash key %s' % (
                                str(len(low_priority_metrics_to_remove)),
                                low_priority_metrics_hash_key))
                            self.redis_conn.hdel(low_priority_metrics_hash_key, *set(low_priority_metrics_to_remove))
                            logger.info('metrics_manager :: removed %s metrics from the Redis hash key %s' % (
                                str(len(low_priority_metrics_to_remove)),
                                low_priority_metrics_hash_key))
                        except:
                            logger.error(traceback.format_exc())
                            logger.error('error :: metrics_manager :: failed to remove the low_priority_metrics_to_remove the Redis hash key %s' % (
                                low_priority_metrics_hash_key))
                        try:
                            del low_priority_metrics_to_remove
                        except:
                            pass
                    else:
                        logger.info('metrics_manager :: no metrics need to be removed from the Redis hash key %s' % (
                            low_priority_metrics_hash_key))

        spin_end = time() - spin_start
        logger.info('metrics_manager :: metric_management_process took %.2f seconds' % spin_end)
        return

    def run(self):
        """
        - Called when the process intializes.

        - Determine if Redis is up

        - Spawn a process to manage metrics lists and Redis sets

        - Wait for the process to finish.

        - Log the details about the run to the skyline analyzer log.

        - Send skyline.analyzer.metrics_manager metrics to `GRAPHITE_HOST`
        """

        # Log management to prevent overwriting
        # Allow the bin/<skyline_app>.d to manage the log
        now = time()
        log_wait_for = now + 5
        while now < log_wait_for:
            if os.path.isfile(skyline_app_loglock):
                sleep(.1)
                now = time()
            else:
                now = log_wait_for + 1

        logger.info('metrics_manager :: starting %s metrics_manager' % skyline_app)

        # @added 20190417 - Feature #2950: Report defaulted settings to log
        # Added all the globally declared settings to enable reporting in the
        # log the state of each setting.
        try:
            SERVER_METRIC_PATH = '.%s' % settings.SERVER_METRICS_NAME
            if SERVER_METRIC_PATH == '.':
                SERVER_METRIC_PATH = ''
        except:
            SERVER_METRIC_PATH = ''
        try:
            ANALYZER_ENABLED = settings.ANALYZER_ENABLED
            logger.info('metrics_manager :: ANALYZER_ENABLED is set to %s' % str(ANALYZER_ENABLED))
        except:
            ANALYZER_ENABLED = True
            logger.info('warning :: metrics_manager :: ANALYZER_ENABLED is not declared in settings.py, defaults to True')

        while 1:
            now = time()

            # Make sure Redis is up
            try:
                self.redis_conn.ping()
            except:
                logger.error('error :: metrics_manager cannot connect to redis at socket path %s' % settings.REDIS_SOCKET_PATH)
                logger.info(traceback.format_exc())
                sleep(10)
                try:
                    self.redis_conn = get_redis_conn(skyline_app)
                    self.redis_conn_decoded = get_redis_conn_decoded(skyline_app)
                except:
                    logger.info(traceback.format_exc())
                    logger.error('error :: metrics_manager cannot connect to get_redis_conn')
                continue

            # Report app up
            try:
                self.redis_conn.setex('analyzer.metrics_manager', 120, now)
            except:
                logger.error(traceback.format_exc())
                logger.error('error :: metrics_manager :: could not update the Redis analyzer.metrics_manager key')

            # Discover unique metrics
            unique_metrics_count = 0
            try:
                raw_unique_metrics_count = self.redis_conn_decoded.scard(full_uniques)
                unique_metrics_count = int(raw_unique_metrics_count)
            except:
                logger.error(traceback.format_exc())
                logger.error('error :: metrics_manager ::: could not get the count of %s from Redis' % full_uniques)
                sleep(10)
                continue

            if unique_metrics_count == 0:
                logger.info('metrics_manager :: no metrics in redis. try adding some - see README')
                sleep(10)
                continue

            # Spawn processes
            pids = []
            spawned_pids = []
            pid_count = 0
            try:
                p = Process(target=self.metric_management_process, args=(0,))
                pids.append(p)
                pid_count += 1
                logger.info('metrics_manager :: starting metric_management_process')
                p.start()
                spawned_pids.append(p.pid)
            except:
                logger.error(traceback.format_exc())
                logger.error('error :: metrics_manager :: failed to spawn process')

            # Send wait signal to zombie processes
            # for p in pids:
            #     p.join()
            # Self monitor processes and terminate if any metric_management_process has run
            # for longer than 180 seconds - 20160512 @earthgecko
            p_starts = time()
            # TESTING p.join removal
            # while time() - p_starts <= 1:
            while time() - p_starts <= 300:
                if any(p.is_alive() for p in pids):
                    # Just to avoid hogging the CPU
                    sleep(.1)
                else:
                    # All the processes are done, break now.
                    time_to_run = time() - p_starts
                    logger.info('metrics_manager :: metric_management_process completed in %.2f seconds' % (time_to_run))
                    break
            else:
                # We only enter this if we didn't 'break' above.
                logger.info('metrics_manager :: timed out, killing metric_management_process process')
                for p in pids:
                    logger.info('metrics_manager :: killing metric_management_process process')
                    p.terminate()
                    # p.join()
                    logger.info('metrics_manager :: killed metric_management_process process')

            for p in pids:
                if p.is_alive():
                    try:
                        logger.info('metrics_manager :: stopping metric_management_process - %s' % (str(p.is_alive())))
                        # p.join()
                        p.terminate()
                    except:
                        logger.error(traceback.format_exc())
                        logger.error('error :: metrics_manager :: failed to stop spawn process')

            process_runtime = time() - now
            if process_runtime < 60:
                sleep_for = (60 - process_runtime)
                logger.info('metrics_manager :: sleeping for %.2f seconds due to low run time...' % sleep_for)
                sleep(sleep_for)
                try:
                    del sleep_for
                except:
                    logger.error('error :: metrics_manager :: failed to del sleep_for')
            try:
                del process_runtime
            except:
                logger.error('error :: metrics_manager :: failed to del process_runtime')
