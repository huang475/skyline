import logging
from os import path
import string
import operator
import time
import re

import traceback
from flask import request
# import mysql.connector
# from mysql.connector import errorcode

# @added 20180720 - Feature #2464: luminosity_remote_data
# Added redis and msgpack
from redis import StrictRedis
from msgpack import Unpacker

# @added 20201103 - Feature #3824: get_cluster_data
import requests

# @added 20201125 - Feature #3850: webapp - yhat_values API endoint
import numpy as np

import settings
from skyline_functions import (
    mysql_select,
    # @added 20180720 - Feature #2464: luminosity_remote_data
    # nonNegativeDerivative, in_list, is_derivative_metric,
    # @added 20200507 - Feature #3532: Sort all time series
    # Added sort_timeseries and removed unused in_list
    nonNegativeDerivative, is_derivative_metric, sort_timeseries,
    # @added 20201123 - Feature #3824: get_cluster_data
    #                   Feature #2464: luminosity_remote_data
    #                   Bug #3266: py3 Redis binary objects not strings
    #                   Branch #3262: py3
    get_redis_conn_decoded,
    # @added 20201125 - Feature #3850: webapp - yhat_values API endoint
    get_graphite_metric)

import skyline_version
skyline_version = skyline_version.__absolute_version__

skyline_app = 'webapp'
skyline_app_logger = '%sLog' % skyline_app
logger = logging.getLogger(skyline_app_logger)
skyline_app_logfile = '%s/%s.log' % (settings.LOG_PATH, skyline_app)
logfile = '%s/%s.log' % (settings.LOG_PATH, skyline_app)

REQUEST_ARGS = ['from_date',
                'from_time',
                'from_timestamp',
                'until_date',
                'until_time',
                'until_timestamp',
                'target',
                'like_target',
                'source',
                'host',
                'algorithm',
                # @added 20161127 - Branch #922: ionosphere
                'panorama_anomaly_id',
                ]

# Converting one settings variable into a local variable, just because it is a
# long string otherwise.
try:
    ENABLE_WEBAPP_DEBUG = settings.ENABLE_WEBAPP_DEBUG
except:
    logger.error('error :: cannot determine ENABLE_WEBAPP_DEBUG from settings')
    ENABLE_WEBAPP_DEBUG = False

# @added 20180720 - Feature #2464: luminosity_remote_data
# Added REDIS_CONN
if settings.REDIS_PASSWORD:
    REDIS_CONN = StrictRedis(password=settings.REDIS_PASSWORD, unix_socket_path=settings.REDIS_SOCKET_PATH)
else:
    REDIS_CONN = StrictRedis(unix_socket_path=settings.REDIS_SOCKET_PATH)


def panorama_request():
    """
    Gets the details of anomalies from the database, using the URL arguments
    that are passed in by the :obj:`request.args` to build the MySQL select
    query string and queries the database, parse the results and creates an
    array of the anomalies that matched the query and creates the
    ``panaroma.json`` file, then returns the array.  The Webapp needs both the
    array and the JSONP file to serve to the browser for the client side
    ``panaroma.js``.

    :param None: determined from :obj:`request.args`
    :return: array
    :rtype: array

    .. note:: And creates ``panaroma.js`` for client side javascript

    """

    logger.info('determining request args')

    def get_ids_from_rows(thing, rows):
        found_ids = []
        for row in rows:
            found_id = str(row[0])
            found_ids.append(int(found_id))

        # @modified 20191014 - Task #3270: Deprecate string.replace for py3
        #                      Branch #3262: py3
        # ids_first = string.replace(str(found_ids), '[', '')
        # in_ids = string.replace(str(ids_first), ']', '')
        found_ids_str = str(found_ids)
        ids_first = found_ids_str.replace('[', '')
        in_ids = ids_first.replace(']', '')

        return in_ids

    try:
        request_args_len = len(request.args)
    except:
        request_args_len = False

    latest_anomalies = False
    if request_args_len == 0:
        request_args_len = 'No request arguments passed'
        # return str(request_args_len)
        latest_anomalies = True

    metric = False
    if metric:
        logger.info('Getting db id for %s' % metric)
        # @modified 20170913 - Task #2160: Test skyline with bandit
        # Added nosec to exclude from bandit tests
        query = 'select id from metrics WHERE metric=\'%s\'' % metric  # nosec
        try:
            result = mysql_select(skyline_app, query)
        except:
            logger.error('error :: failed to get id from db: %s' % traceback.format_exc())
            result = 'metric id not found in database'

        return str(result[0][0])

    search_request = True
    count_request = False

    if latest_anomalies:
        logger.info('Getting latest anomalies')
        # @modified 20191108 - Feature #3306: Record the anomaly_end_timestamp
        #                      Branch #3262: py3
        # query = 'select id, metric_id, anomalous_datapoint, anomaly_timestamp, full_duration, created_timestamp from anomalies ORDER BY id DESC LIMIT 10'
        query = 'select id, metric_id, anomalous_datapoint, anomaly_timestamp, full_duration, created_timestamp, anomaly_end_timestamp from anomalies ORDER BY id DESC LIMIT 10'
        try:
            rows = mysql_select(skyline_app, query)
        except:
            logger.error('error :: failed to get anomalies from db: %s' % traceback.format_exc())
            rows = []

    if not latest_anomalies:
        logger.info('Determining search parameters')
        # @modified 20191108 - Feature #3306: Record the end_timestamp of anomalies
        #                      Branch #3262: py3
        # query_string = 'select id, metric_id, anomalous_datapoint, anomaly_timestamp, full_duration, created_timestamp from anomalies'
        query_string = 'select id, metric_id, anomalous_datapoint, anomaly_timestamp, full_duration, created_timestamp, anomaly_end_timestamp from anomalies'

        needs_and = False

        # If we have to '' a string we cannot escape the query it seems...
        do_not_escape = False
        if 'metric' in request.args:
            metric = request.args.get('metric', None)
            if metric and metric != 'all':
                # @modified 20170913 - Task #2160: Test skyline with bandit
                # Added nosec to exclude from bandit tests
                query = "select id from metrics WHERE metric='%s'" % (metric)  # nosec
                try:
                    found_id = mysql_select(skyline_app, query)
                except:
                    logger.error('error :: failed to get app ids from db: %s' % traceback.format_exc())
                    found_id = None

                if found_id:
                    target_id = str(found_id[0][0])
                    if needs_and:
                        new_query_string = '%s AND metric_id=%s' % (query_string, target_id)
                    else:
                        new_query_string = '%s WHERE metric_id=%s' % (query_string, target_id)
                    query_string = new_query_string
                    needs_and = True

        if 'metric_like' in request.args:
            metric_like = request.args.get('metric_like', None)
            if metric_like and metric_like != 'all':
                # @modified 20170913 - Task #2160: Test skyline with bandit
                # Added nosec to exclude from bandit tests
                query = 'select id from metrics WHERE metric LIKE \'%s\'' % (str(metric_like))  # nosec
                try:
                    rows = mysql_select(skyline_app, query)
                except:
                    logger.error('error :: failed to get metric ids from db: %s' % traceback.format_exc())
                    return False

                rows_returned = None
                try:
                    rows_returned = rows[0]
                    if ENABLE_WEBAPP_DEBUG:
                        logger.info('debug :: rows - rows[0] - %s' % str(rows[0]))
                except:
                    rows_returned = False
                    if ENABLE_WEBAPP_DEBUG:
                        logger.info('debug :: no rows returned')

                if rows_returned:
                    ids = get_ids_from_rows('metric', rows)
                    new_query_string = '%s WHERE metric_id IN (%s)' % (query_string, str(ids))
                else:
                    # Get nothing
                    new_query_string = '%s WHERE metric_id IN (0)' % (query_string)
                    if ENABLE_WEBAPP_DEBUG:
                        logger.info('debug :: no rows returned using new_query_string - %s' % new_query_string)

                query_string = new_query_string
                needs_and = True

        if 'count_by_metric' in request.args:
            count_by_metric = request.args.get('count_by_metric', None)
            if count_by_metric and count_by_metric != 'false':
                search_request = False
                count_request = True
                # query_string = 'SELECT metric_id, COUNT(*) FROM anomalies GROUP BY metric_id ORDER BY COUNT(*) DESC'
                query_string = 'SELECT metric_id, COUNT(*) FROM anomalies'
                needs_and = False

        if 'from_timestamp' in request.args:
            from_timestamp = request.args.get('from_timestamp', None)
            if from_timestamp and from_timestamp != 'all':

                if ":" in from_timestamp:
                    import time
                    import datetime
                    new_from_timestamp = time.mktime(datetime.datetime.strptime(from_timestamp, '%Y%m%d %H:%M').timetuple())
                    from_timestamp = str(int(new_from_timestamp))

                if needs_and:
                    new_query_string = '%s AND anomaly_timestamp >= %s' % (query_string, from_timestamp)
                    query_string = new_query_string
                    needs_and = True
                else:
                    new_query_string = '%s WHERE anomaly_timestamp >= %s' % (query_string, from_timestamp)
                    query_string = new_query_string
                    needs_and = True

        if 'until_timestamp' in request.args:
            until_timestamp = request.args.get('until_timestamp', None)
            if until_timestamp and until_timestamp != 'all':
                if ":" in until_timestamp:
                    import time
                    import datetime
                    new_until_timestamp = time.mktime(datetime.datetime.strptime(until_timestamp, '%Y%m%d %H:%M').timetuple())
                    until_timestamp = str(int(new_until_timestamp))

                if needs_and:
                    new_query_string = '%s AND anomaly_timestamp <= %s' % (query_string, until_timestamp)
                    query_string = new_query_string
                    needs_and = True
                else:
                    new_query_string = '%s WHERE anomaly_timestamp <= %s' % (query_string, until_timestamp)
                    query_string = new_query_string
                    needs_and = True

        if 'app' in request.args:
            app = request.args.get('app', None)
            if app and app != 'all':
                # @modified 20170913 - Task #2160: Test skyline with bandit
                # Added nosec to exclude from bandit tests
                query = 'select id from apps WHERE app=\'%s\'' % (str(app))  # nosec
                try:
                    found_id = mysql_select(skyline_app, query)
                except:
                    logger.error('error :: failed to get app ids from db: %s' % traceback.format_exc())
                    found_id = None

                if found_id:
                    target_id = str(found_id[0][0])
                    if needs_and:
                        new_query_string = '%s AND app_id=%s' % (query_string, target_id)
                    else:
                        new_query_string = '%s WHERE app_id=%s' % (query_string, target_id)

                    query_string = new_query_string
                    needs_and = True

        if 'source' in request.args:
            source = request.args.get('source', None)
            if source and source != 'all':
                # @modified 20170913 - Task #2160: Test skyline with bandit
                # Added nosec to exclude from bandit tests
                query = 'select id from sources WHERE source=\'%s\'' % (str(source))  # nosec
                try:
                    found_id = mysql_select(skyline_app, query)
                except:
                    logger.error('error :: failed to get source id from db: %s' % traceback.format_exc())
                    found_id = None

                if found_id:
                    target_id = str(found_id[0][0])
                    if needs_and:
                        new_query_string = '%s AND source_id=\'%s\'' % (query_string, target_id)
                    else:
                        new_query_string = '%s WHERE source_id=\'%s\'' % (query_string, target_id)

                    query_string = new_query_string
                    needs_and = True

        if 'algorithm' in request.args:
            algorithm = request.args.get('algorithm', None)

            # DISABLED as it is difficult match algorithm_id in the
            # triggered_algorithms csv list
            algorithm = 'all'
            if algorithm and algorithm != 'all':
                # @modified 20170913 - Task #2160: Test skyline with bandit
                # Added nosec to exclude from bandit tests
                query = 'select id from algorithms WHERE algorithm LIKE \'%s\'' % (str(algorithm))  # nosec
                try:
                    rows = mysql_select(skyline_app, query)
                except:
                    logger.error('error :: failed to get algorithm ids from db: %s' % traceback.format_exc())
                    rows = []

                ids = get_ids_from_rows('algorithm', rows)

                if needs_and:
                    new_query_string = '%s AND algorithm_id IN (%s)' % (query_string, str(ids))
                else:
                    new_query_string = '%s WHERE algorithm_id IN (%s)' % (query_string, str(ids))
                query_string = new_query_string
                needs_and = True

        if 'host' in request.args:
            host = request.args.get('host', None)
            if host and host != 'all':
                # @modified 20170913 - Task #2160: Test skyline with bandit
                # Added nosec to exclude from bandit tests
                query = 'select id from hosts WHERE host=\'%s\'' % (str(host))  # nosec
                try:
                    found_id = mysql_select(skyline_app, query)
                except:
                    logger.error('error :: failed to get host id from db: %s' % traceback.format_exc())
                    found_id = None

                if found_id:
                    target_id = str(found_id[0][0])
                    if needs_and:
                        new_query_string = '%s AND host_id=\'%s\'' % (query_string, target_id)
                    else:
                        new_query_string = '%s WHERE host_id=\'%s\'' % (query_string, target_id)
                    query_string = new_query_string
                    needs_and = True

        if 'limit' in request.args:
            limit = request.args.get('limit', '10')
        else:
            limit = '10'

        if 'order' in request.args:
            order = request.args.get('order', 'DESC')
        else:
            order = 'DESC'

        search_query = '%s ORDER BY id %s LIMIT %s' % (
            query_string, order, limit)

        if 'count_by_metric' in request.args:
            count_by_metric = request.args.get('count_by_metric', None)
            if count_by_metric and count_by_metric != 'false':
                # query_string = 'SELECT metric_id, COUNT(*) FROM anomalies GROUP BY metric_id ORDER BY COUNT(*) DESC'
                search_query = '%s GROUP BY metric_id ORDER BY COUNT(*) %s LIMIT %s' % (
                    query_string, order, limit)

        try:
            rows = mysql_select(skyline_app, search_query)
        except:
            logger.error('error :: failed to get anomalies from db: %s' % traceback.format_exc())
            rows = []

    anomalies = []
    anomalous_metrics = []

    if search_request:
        # @modified 20191014 - Task #3270: Deprecate string.replace for py3
        #                      Branch #3262: py3
        anomalies_json = path.abspath(path.join(path.dirname(__file__), '..', settings.ANOMALY_DUMP))
        # panorama_json = string.replace(str(anomalies_json), 'anomalies.json', 'panorama.json')
        panorama_json = anomalies_json.replace('anomalies.json', 'panorama.json')
        if ENABLE_WEBAPP_DEBUG:
            logger.info('debug ::  panorama_json - %s' % str(panorama_json))

    for row in rows:
        if search_request:
            anomaly_id = str(row[0])
            metric_id = str(row[1])
        if count_request:
            metric_id = str(row[0])
            anomaly_count = str(row[1])

        # @modified 20170913 - Task #2160: Test skyline with bandit
        # Added nosec to exclude from bandit tests
        query = 'select metric from metrics WHERE id=%s' % metric_id  # nosec
        try:
            result = mysql_select(skyline_app, query)
        except:
            logger.error('error :: failed to get id from db: %s' % traceback.format_exc())
            continue

        metric = str(result[0][0])
        if search_request:
            anomalous_datapoint = str(row[2])
            anomaly_timestamp = str(row[3])
            anomaly_timestamp = str(row[3])
            full_duration = str(row[4])
            created_timestamp = str(row[5])
            # @modified 20191108 - Feature #3306: Record the anomaly_end_timestamp
            #                      Branch #3262: py3
            # anomaly_data = (anomaly_id, metric, anomalous_datapoint, anomaly_timestamp, full_duration, created_timestamp)
            # anomalies.append([int(anomaly_id), str(metric), anomalous_datapoint, anomaly_timestamp, full_duration, created_timestamp])
            anomaly_end_timestamp = str(row[6])
            anomaly_data = (anomaly_id, metric, anomalous_datapoint, anomaly_timestamp, full_duration, created_timestamp, anomaly_end_timestamp)
            anomalies.append([int(anomaly_id), str(metric), anomalous_datapoint, anomaly_timestamp, full_duration, created_timestamp, anomaly_end_timestamp])
            anomalous_metrics.append(str(metric))

        if count_request:
            limit_argument = anomaly_count
            if int(anomaly_count) > 100:
                limit_argument = 100
            anomaly_data = (int(anomaly_count), metric, str(limit_argument))
            anomalies.append([int(anomaly_count), str(metric), str(limit_argument)])

    anomalies.sort(key=operator.itemgetter(int(0)))

    if search_request:
        with open(panorama_json, 'w') as fh:
            pass

        # Write anomalous_metrics to static webapp directory
        with open(panorama_json, 'a') as fh:
            # Make it JSONP with a handle_data() function
            fh.write('handle_data(%s)' % anomalies)

    if latest_anomalies:
        return anomalies
    else:
        return search_query, anomalies


def get_list(thing):
    """
    Get a list of names for things in a database table.

    :param thing: the thing, e.g. 'algorithm'
    :type thing: str
    :return: list
    :rtype: list

    """
    table = '%ss' % thing
    # @modified 20170913 - Task #2160: Test skyline with bandit
    # Added nosec to exclude from bandit tests
    query = 'select %s from %s' % (thing, table)  # nosec
    logger.info('select %s from %s' % (thing, table))  # nosec
    got_results = False
    try:
        results = mysql_select(skyline_app, query)
        got_results = True
    except:
        logger.error('error :: failed to get list of %ss from %s' % (thing, table))
        results = None

    things = []
    results_array_valid = False
    try:
        test_results = results[0]
        results_array_valid = True
    except:
        logger.error('error :: invalid results array for get list of %ss from %s' % (thing, table))

    if results_array_valid:
        logger.info('results: %s' % str(results))
        for result in results:
            things.append(str(result[0]))
        logger.info('things: %s' % str(things))

    return things


# @added 20180720 - Feature #2464: luminosity_remote_data
# @modified 20201203 - Feature #3860: luminosity - handle low frequency data
# Add the metric resolution
# def luminosity_remote_data(anomaly_timestamp):
def luminosity_remote_data(anomaly_timestamp, resolution):
    """
    Gets all the unique_metrics from Redis and then mgets Redis data for all
    metrics.  The data is then preprocessed for the remote Skyline luminosity
    instance and only the relevant fragments of the time series are
    returned.  This return is then gzipped by the Flask Webapp response to
    ensure the minimum about of bandwidth is used.

    :param anomaly_timestamp: the anomaly timestamp
    :type anomaly_timestamp: int
    :return: list
    :rtype: list

    """

    message = 'luminosity_remote_data returned'
    success = False
    luminosity_data = []
    logger.info('luminosity_remote_data :: determining unique_metrics')
    unique_metrics = []
    # If you modify the values of 61 or 600 here, it must be modified in the
    # luminosity_remote_data function in
    # skyline/luminosity/process_correlations.py as well
    # @modified 20201203 - Feature #3860: luminosity - handle low frequency data
    # Use the metric resolution
    # from_timestamp = int(anomaly_timestamp) - 600
    # until_timestamp = int(anomaly_timestamp) + 61
    from_timestamp = int(anomaly_timestamp) - (resolution * 10)
    until_timestamp = int(anomaly_timestamp) + (resolution + 1)

    try:
        # @modified 20201123 - Feature #3824: get_cluster_data
        #                      Feature #2464: luminosity_remote_data
        #                      Bug #3266: py3 Redis binary objects not strings
        #                      Branch #3262: py3
        # unique_metrics = list(REDIS_CONN.smembers(settings.FULL_NAMESPACE + 'unique_metrics'))
        REDIS_CONN_DECODED = get_redis_conn_decoded(skyline_app)
        unique_metrics = list(REDIS_CONN_DECODED.smembers(settings.FULL_NAMESPACE + 'unique_metrics'))
    except Exception as e:
        logger.error('error :: %s' % str(e))
        logger.error('error :: luminosity_remote_data :: could not determine unique_metrics from Redis set')
    if not unique_metrics:
        message = 'error :: luminosity_remote_data :: could not determine unique_metrics from Redis set'
        return luminosity_data, success, message
    logger.info('luminosity_remote_data :: %s unique_metrics' % str(len(unique_metrics)))

    # assigned metrics
    assigned_min = 0
    assigned_max = len(unique_metrics)
    assigned_keys = range(assigned_min, assigned_max)

    # Compile assigned metrics
    assigned_metrics = [unique_metrics[index] for index in assigned_keys]
    # Check if this process is unnecessary
    if len(assigned_metrics) == 0:
        message = 'error :: luminosity_remote_data :: assigned_metrics length is 0'
        logger.error(message)
        return luminosity_data, success, message

    # Multi get series
    raw_assigned_failed = True
    try:
        raw_assigned = REDIS_CONN.mget(assigned_metrics)
        raw_assigned_failed = False
    except:
        logger.info(traceback.format_exc())
        message = 'error :: luminosity_remote_data :: failed to mget raw_assigned'
        logger.error(message)
        return luminosity_data, success, message
    if raw_assigned_failed:
        message = 'error :: luminosity_remote_data :: failed to mget raw_assigned'
        logger.error(message)
        return luminosity_data, success, message

    # Distill timeseries strings into lists
    for i, metric_name in enumerate(assigned_metrics):
        timeseries = []
        try:
            raw_series = raw_assigned[i]
            unpacker = Unpacker(use_list=False)
            unpacker.feed(raw_series)
            timeseries = list(unpacker)
        except:
            timeseries = []

        if not timeseries:
            continue

        # @added 20200507 - Feature #3532: Sort all time series
        # To ensure that there are no unordered timestamps in the time
        # series which are artefacts of the collector or carbon-relay, sort
        # all time series by timestamp before analysis.
        original_timeseries = timeseries
        if original_timeseries:
            timeseries = sort_timeseries(original_timeseries)
            del original_timeseries

        # Convert the time series if this is a known_derivative_metric
        # @modified 20200728 - Bug #3652: Handle multiple metrics in base_name conversion
        # base_name = metric_name.replace(settings.FULL_NAMESPACE, '', 1)

        # @added 20201117 - Feature #3824: get_cluster_data
        #                   Feature #2464: luminosity_remote_data
        #                   Bug #3266: py3 Redis binary objects not strings
        #                   Branch #3262: py3
        # Convert metric_name bytes to str
        metric_name = str(metric_name)

        if metric_name.startswith(settings.FULL_NAMESPACE):
            base_name = metric_name.replace(settings.FULL_NAMESPACE, '', 1)
        else:
            base_name = metric_name

        known_derivative_metric = is_derivative_metric('webapp', base_name)
        if known_derivative_metric:
            try:
                derivative_timeseries = nonNegativeDerivative(timeseries)
                timeseries = derivative_timeseries
            except:
                logger.error('error :: nonNegativeDerivative failed')

        correlate_ts = []
        for ts, value in timeseries:
            if int(ts) < from_timestamp:
                continue
            if int(ts) <= anomaly_timestamp:
                correlate_ts.append((int(ts), value))
            if int(ts) > (anomaly_timestamp + until_timestamp):
                break
        if not correlate_ts:
            continue
        metric_data = [str(metric_name), correlate_ts]
        luminosity_data.append(metric_data)

    logger.info('luminosity_remote_data :: %s valid metric time series data preprocessed for the remote request' % str(len(luminosity_data)))

    return luminosity_data, success, message


# @added 20200908 - Feature #3740: webapp - anomaly API endpoint
def panorama_anomaly_details(anomaly_id):
    """
    Gets the details for an anomaly from the database.
    """

    logger.info('panorama_anomaly_details - getting details for anomaly id %s' % str(anomaly_id))

    metric_id = 0
    # Added nosec to exclude from bandit tests
    query = 'select metric_id from anomalies WHERE id=\'%s\'' % str(anomaly_id)  # nosec
    try:
        result = mysql_select(skyline_app, query)
        metric_id = int(result[0][0])
    except:
        logger.error(traceback.format_exc())
        logger.error('error :: panorama_anomaly_details - failed to get metric_id from db')
        return False
    if metric_id > 0:
        logger.info('panorama_anomaly_details - getting metric for metric_id - %s' % str(metric_id))
        # Added nosec to exclude from bandit tests
        query = 'select metric from metrics WHERE id=\'%s\'' % str(metric_id)  # nosec
        try:
            result = mysql_select(skyline_app, query)
            metric = str(result[0][0])
        except:
            logger.error(traceback.format_exc())
            logger.error('error :: panorama_anomaly_details - failed to get metric from db')
            return False
    query = 'select id, metric_id, anomalous_datapoint, anomaly_timestamp, full_duration, created_timestamp, anomaly_end_timestamp from anomalies WHERE id=\'%s\'' % str(anomaly_id)  # nosec
    logger.info('panorama_anomaly_details - running query - %s' % str(query))
    try:
        rows = mysql_select(skyline_app, query)
    except:
        logger.error(traceback.format_exc())
        logger.error('error :: panorama_anomaly_details - failed to get anomaly details from db')
        return False
    anomaly_data = None
    for row in rows:
        anomalous_datapoint = float(row[2])
        anomaly_timestamp = int(row[3])
        full_duration = int(row[4])
        created_timestamp = str(row[5])
        try:
            anomaly_end_timestamp = int(row[6])
        except:
            anomaly_end_timestamp = None
        anomaly_data = [int(anomaly_id), str(metric), anomalous_datapoint, anomaly_timestamp, full_duration, created_timestamp, anomaly_end_timestamp]
        break
    return anomaly_data


# @added 20201103 - Feature #3824: get_cluster_data
# @modified 20201127 - Feature #3824: get_cluster_data
#                      Feature #3820: HORIZON_SHARDS
# Allow to query only a single host in the cluster so that just the response
# can from a single host in the cluster can be evaluated
def get_cluster_data(api_endpoint, data_required, only_host='all', endpoint_params={}):
    """
    Gets data from the /api of REMOTE_SKYLINE_INSTANCES.  This allows the user
    to query a single Skyline webapp node in a cluster and the Skyline instance
    will respond with the concentated responses of all the
    REMOTE_SKYLINE_INSTANCES in one a single response.

    :param api_endpoint: the api endpoint to request data from the remote
        Skyline instances
    :param data_required: the element from the api json response that is
        required
    :param only_host: The remote Skyline host to query, if not passed all are
        queried.
    :param endpoint_params: A dictionary of any additional parameters that may
        be required
    :type api_endpoint: str
    :type data_required: str
    :type only_host: str
    :type endpoint_params: dict
    :return: list
    :rtype: list

    """
    try:
        connect_timeout = int(settings.GRAPHITE_CONNECT_TIMEOUT)
        read_timeout = int(settings.GRAPHITE_READ_TIMEOUT)
    except:
        connect_timeout = 5
        read_timeout = 10
    use_timeout = (int(connect_timeout), int(read_timeout))
    data = []

    if only_host != 'all':
        logger.info('get_cluster_data :: querying all remote hosts as only_host set to %s' % (
            str(only_host)))

    for item in settings.REMOTE_SKYLINE_INSTANCES:
        r = None
        user = None
        password = None
        use_auth = False

        # @added 20201127 - Feature #3824: get_cluster_data
        #                   Feature #3820: HORIZON_SHARDS
        # Allow to query only a single host in the cluster so that just the response
        # can from a single host in the cluster can be evaluated
        if only_host != 'all':
            if only_host != str(item[0]):
                logger.info('get_cluster_data :: not querying %s as only_host set to %s' % (
                    str(item[0]), str(only_host)))
                continue
            else:
                logger.info('get_cluster_data :: querying %s as only_host set to %s' % (
                    str(item[0]), str(only_host)))

        try:
            user = str(item[1])
            password = str(item[2])
            use_auth = True
        except:
            user = None
            password = None
        logger.info('get_cluster_data :: querying %s for %s on %s' % (
            str(item[0]), str(data_required), str(api_endpoint)))
        try:
            url = '%s/api?%s' % (str(item[0]), api_endpoint)
            if use_auth:
                r = requests.get(url, timeout=use_timeout, auth=(user, password))
            else:
                r = requests.get(url, timeout=use_timeout)
        except:
            logger.error(traceback.format_exc())
            logger.error('error :: get_cluster_data :: failed to %s from %s' % (
                api_endpoint, str(item)))
        if r:
            if r.status_code != 200:
                logger.error('error :: get_cluster_data :: %s from %s responded with status code %s and reason %s' % (
                    api_endpoint, str(item), str(r.status_code), str(r.reason)))
            js = None
            try:
                js = r.json()
            except:
                logger.error(traceback.format_exc())
                logger.error('error :: get_cluster_data :: failed to get json from the response from %s on %s' % (
                    api_endpoint, str(item)))
            remote_data = []
            if js:
                logger.info('get_cluster_data :: got response for %s from %s' % (
                    str(data_required), str(item[0])))
                try:
                    remote_data = js['data'][data_required]
                except:
                    logger.error(traceback.format_exc())
                    logger.error('error :: get_cluster_data :: failed to build remote_data from %s on %s' % (
                        str(data_required), str(item)))
            if remote_data:
                logger.info('get_cluster_data :: got %s %s from %s' % (
                    str(len(remote_data)), str(data_required), str(item[0])))
                data = data + remote_data
    return data


# @added 20201125 - Feature #3850: webapp - yhat_values API endoint
def get_yhat_values(
        metric, from_timestamp, until_timestamp, include_value, include_mean,
        include_yhat_real_lower):

    timeseries = []
    try:
        logger.info('get_yhat_values :: for %s from %s until %s' % (
            metric, str(from_timestamp), str(until_timestamp)))
        timeseries = get_graphite_metric('webapp', metric, from_timestamp, until_timestamp, 'list', 'object')
    except:
        logger.error(traceback.format_exc())
        logger.error('error :: get_yhat_values :: failed to get timeseries data for %s' % (
            metric))
        return None

    yhat_dict = {}
    logger.info('get_yhat_values :: %s values in timeseries for %s to calculate yhat values from' % (
        str(len(timeseries)), metric))
    if timeseries:
        try:
            array_amin = np.amin([item[1] for item in timeseries])
            values = []
            for ts, value in timeseries:
                values.append(value)
                va = np.array(values)
                va_mean = va.mean()
                va_std_3 = 3 * va.std()
                # @modified 20201126 - Feature #3850: webapp - yhat_values API endoint
                # Change dict key to int not float
                int_ts = int(ts)
                yhat_dict[int_ts] = {}
                if include_value:
                    yhat_dict[int_ts]['value'] = value
                if include_mean:
                    yhat_dict[int_ts]['mean'] = va_mean
                if include_mean:
                    yhat_dict[int_ts]['mean'] = va_mean
                yhat_lower = va_mean - va_std_3
                if include_yhat_real_lower:
                    # @modified 20201202 - Feature #3850: webapp - yhat_values API endoint
                    # Set the yhat_real_lower correctly
                    # if yhat_lower < array_amin and array_amin == 0:
                    #     yhat_dict[int_ts]['yhat_real_lower'] = array_amin
                    if yhat_lower < 0 and array_amin > -0.0000000001:
                        yhat_dict[int_ts]['yhat_real_lower'] = 0
                    else:
                        yhat_dict[int_ts]['yhat_real_lower'] = yhat_lower
                yhat_dict[int_ts]['yhat_lower'] = yhat_lower
                yhat_dict[int_ts]['yhat_upper'] = va_mean + va_std_3
        except:
            logger.error(traceback.format_exc())
            logger.error('error :: get_yhat_values :: failed create yhat_dict for %s' % (
                metric))
            return None

    logger.info('get_yhat_values :: calculated yhat values for %s data points' % str(len(yhat_dict)))
    if yhat_dict:
        yhat_dict_cache_key = 'webapp.%s.%s.%s.%s.%s.%s' % (
            metric, str(from_timestamp), str(until_timestamp),
            str(include_value), str(include_mean),
            str(include_yhat_real_lower))
        logger.info('get_yhat_values :: saving yhat_dict to Redis key - %s' % yhat_dict_cache_key)
        try:
            REDIS_CONN.setex(yhat_dict_cache_key, 14400, str(yhat_dict))
            logger.info('get_yhat_values :: created Redis key - %s with 14400 TTL' % yhat_dict_cache_key)
        except:
            logger.error(traceback.format_exc())
            logger.error('error :: get_yhat_values :: failed to setex Redis key - %s' % yhat_dict_cache_key)

    return yhat_dict
