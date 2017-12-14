#!/usr/bin/env python2
#
# (c) Copyright 2015-2017 Hewlett Packard Enterprise Development LP
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
"""
Tool for gathering job data from a Jenkins server and generating summary
stat reports and graphs. These reports cover metrics such as success/failure
rates and job duration.

The job data is persisted in a file as JSON lines (JSONL) (see
https://en.wikipedia.org/wiki/JSON_Streaming#Line_delimited_JSON for more
details).

The script can be run as follows,

./get_jenkins_stats.py -s <jenkins url> -j <jenkins job name> -o <output directory>

and

 cat <output directory>/<jenkins job name>.json | jq

to make JSONL readable for debugging

"""

import argparse
from datetime import datetime
from datetime import timedelta
import fcntl
import jinja2
import json
import logging
import os
import pandas as pd
import plotly
from plotly import graph_objs as go
import requests
import sys
import time
    
LOCK_RETRIES = 10
RETRY_SLEEP_SEC = 5

log = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser()
    script_name = __file__
    parser.set_defaults(
            script_dir=os.path.abspath((os.path.dirname(script_name))))
    parser.add_argument('-s', '--server', dest='jenkins_server')
    parser.add_argument('-j', '--job', dest='jenkins_job')
    parser.add_argument('-o', '--output-dir', dest='output_dir',
                        default='/var/www/html/jenkins/stats/',
                        help='Path to output files to (def: %(default)s).')
    parser.add_argument('-k', '--secure', dest='verify_https_requests',
                        action='store_true',
                        help='Verify https requests (def: %(default)s).')
    parser.add_argument('-r', '--report-range-hours', dest='range_hours',
                        type=int, default=336,
                        help='Range for report in hours (def: %(default)s).')
    parser.add_argument('-t', '--html-template', dest='html_template',
                        default=os.path.join(parser.get_default('script_dir'),
                                             'jenkins_stats.html.j2'),
                        help='Jinja2 template to use for html reports '
                             '(def: %(default)s).')
    parser.add_argument('-v', '--verbose', dest='log_verbosely',
                        action='store_true',
                        help='Show DEBUG level log output.')
    parser.add_argument('-q', '--quiet', dest='log_quietly',
                        action='store_true',
                        help='Show only ERROR log messages.')
    parser.add_argument('-l', '--logfile',
                        help='''
                        Name of the file to log messages to
                        (def: %(default)s).
                        ''',
                        default='%s.log' %
                        os.path.splitext(os.path.basename(script_name))[0])
    parser.add_argument('--no-log', dest='no_logfile', action='store_true',
                        help='Do not write log output to file.')
    args = parser.parse_args()
    configure_logging(args)

    # enable logging multiple columns in output
    pd.set_option('display.width', 1000)

    dir_path = os.path.join(args.output_dir)
    if not os.path.exists(dir_path):
        os.makedirs(dir_path)

    # lock the data-file before we do anything else, since we don't want
    # another writer modifying the file after we've read but before we've
    # written (this is all redundant if we use a db)
    data_file = os.path.join(dir_path, '%s.json' % args.jenkins_job)
    global lock_file
    lock_file = '%s.lck' % data_file

    log.debug('Using lock file %s', lock_file)
    if os.path.exists(lock_file):
        lock = open(lock_file, 'r+')
    else:
        lock = open(lock_file, 'w')
    for attempt in xrange(1, LOCK_RETRIES + 1):
        log.debug('Locking attempt %d of %d', attempt, LOCK_RETRIES)
        try:
            fcntl.lockf(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except IOError:
            log.debug('%s already locked, sleep and retry', lock_file)
            time.sleep(RETRY_SLEEP_SEC)
        else:
            log.debug('%s locked, continuing', lock_file)
            # we have the lock, we're done
            break
    else:
        log.critical('Failed to lock file for write, aborting')
        exit(1)

    finish_dt = datetime.now()
    start_dt = finish_dt - timedelta(hours=args.range_hours)
    log.info('Reporting on jobs between %s and %s',
             start_dt.strftime('%H:%M:%S %d-%b-%Y'),
             finish_dt.strftime('%H:%M:%S %d-%b-%Y'))
    builds = get_builds(args, data_file)

    df_builds = builds_to_dataframe(builds)
    df_overall_stats = generate_overall_build_stats(args, df_builds, start_dt)
    slave_stats = generate_per_slave_stats(args, df_builds, start_dt)

    html = generate_html(args, df_overall_stats, slave_stats)
    write_html(args, dir_path, html)


def write_html(args, dir_path, html):
    file_name = '%s.html' % args.jenkins_job
    file_path = os.path.join(dir_path, file_name)
    with open(file_path, 'w') as f:
        f.write(html)
    log.info('Wrote %s', file_path)


def generate_html(args, df_overall_stats, slave_stats):
    with open(args.html_template, 'r') as f:
        html_template = f.read()
    template = jinja2.Template(html_template)
    if args.range_hours <= 24:
        report_units = '%d hours' % args.range_hours
    else:
        report_units = '%g days' % (args.range_hours / 24)
    html = template.render(
        title='%s for last %s' % (args.jenkins_job, report_units),
        status_plot=plot_status(df_overall_stats),
        slave_status_plot=plot_slave_status(slave_stats),
        duration_plot=plot_duration(df_overall_stats),
        slave_duration_plot=plot_slave_duration(slave_stats),
        timestamp_dt=datetime.now(),
        df_stats=df_overall_stats)
    return html


def generate_overall_build_stats(args, df, start_dt):
    log.debug('Generating overall stats')

    # resample data for plots
    # for 24 hours or less, use units of 1 hours, otherwise use units of 1 day
    if args.range_hours <= 24:
        sample_window = '1H'
    else:
        sample_window = '1D'
    df_stats = pd.DataFrame()
    # ignoring aborts from total and pct calc
    df_stats['success'] = df.success.resample(sample_window).sum()
    df_stats['failure'] = df.failure.resample(sample_window).sum()
    df_stats['aborted'] = df.aborted.resample(sample_window).sum()
    df_stats['total'] = df_stats.success + df_stats.failure
    df_stats['success_pct'] = df_stats.success / df_stats.total * 100
    df_stats['failure_pct'] = df_stats.failure / df_stats.total * 100
    df_stats['duration_sec_min'] = df.duration_sec.resample(sample_window).min()
    df_stats['duration_sec_max'] = df.duration_sec.resample(sample_window).max()
    df_stats['duration_sec_avg'] = df.duration_sec.resample(
        sample_window).mean()
    df_success = df[df['success'] == True]
    df_stats['success_duration_min_avg'] = df_success.duration_sec.resample(
        sample_window).mean() / 60
    # resample doesn't currently support percentile
    # (https://github.com/pandas-dev/pandas/issues/15023)
    df_stats['success_duration_min_90th'] = df_success.duration_sec.groupby(
        pd.Grouper(freq=sample_window)).quantile(0.9) / 60
    df_stats['success_duration_min_50th'] = df_success.duration_sec.groupby(
        pd.Grouper(freq=sample_window)).quantile(0.5) / 60
    df_stats = df_stats.round(decimals=1)

    # restrict to builds since start_dt
    df_stats = df_stats[df_stats.index > start_dt]

    df_stats.fillna(value=0, inplace=True)
    log.debug('df_stats:\n%s\n', df_stats)
    return df_stats


def generate_per_slave_stats(args, df, start_dt):
    stats = dict()

    for slave in df.slave.unique():
        log.debug('Generating stats for %s', slave)
        df_slave = df[df['slave'] == slave]
        log.debug('df_slave (%s):\n%s\n', slave, df_slave)

        # resample data for plots
        # for 24 hours or less, use units of 1 hours, otherwise use units of 1 day
        if args.range_hours <= 24:
            sample_window = '1H'
        else:
            sample_window = '1D'
        df_stats = pd.DataFrame()
        # ignoring aborts from total and pct calc
        df_stats['success'] = df_slave.success.resample(sample_window).sum()
        df_stats['failure'] = df_slave.failure.resample(sample_window).sum()
        df_stats['aborted'] = df_slave.aborted.resample(sample_window).sum()
        df_stats['total'] = df_stats.success + df_stats.failure
        df_stats['success_pct'] = df_stats.success / df_stats.total * 100
        df_stats['failure_pct'] = df_stats.failure / df_stats.total * 100
        df_stats['duration_sec_min'] = df_slave.duration_sec.resample(sample_window).min()
        df_stats['duration_sec_max'] = df_slave.duration_sec.resample(sample_window).max()
        df_stats['duration_sec_avg'] = df_slave.duration_sec.resample(
            sample_window).mean()
        df_success = df_slave[df_slave['success'] == True]
        df_stats['success_duration_min_avg'] = df_success.duration_sec.resample(
            sample_window).mean() / 60
        # resample doesn't currently support percentile
        # (https://github.com/pandas-dev/pandas/issues/15023)
        df_stats['success_duration_min_90th'] = df_success.duration_sec.groupby(
            pd.Grouper(freq=sample_window)).quantile(0.9) / 60
        df_stats['success_duration_min_50th'] = df_success.duration_sec.groupby(
            pd.Grouper(freq=sample_window)).quantile(0.5) / 60
        df_stats = df_stats.round(decimals=1)

        # restrict to builds since start_dt
        df_stats = df_stats[df_stats.index > start_dt]

        df_stats.fillna(value=0, inplace=True)
        log.debug('df_stats for %s:\n%s\n', slave, df_stats)

        stats[slave] = df_stats

    return stats


def builds_to_dataframe(builds):
    """
    Convert builds data to pandas dataframe for subsequent analysis
    """

    build_data = dict()
    build_data['timestamp'] = list()
    build_data['success'] = list()
    build_data['failure'] = list()
    build_data['aborted'] = list()
    build_data['duration_sec'] = list()
    build_data['slave'] = list()
    for number in builds:
        build = builds[number]

        # time, success, failure, aborted, duration
        success, failure, aborted = False, False, False
        if build['result'] == 'SUCCESS':
            success = True
        elif build['result'] == 'FAILURE':
            failure = True
        elif build['result'] == 'ABORTED':
            aborted = True
        else:
            log.critical('Unknown status on build %s: %s', number,
                         build['result'])
            exit(1)
        build_data['timestamp'].append(build['start_time'])
        build_data['success'].append(success)
        build_data['failure'].append(failure)
        build_data['aborted'].append(aborted)
        build_data['duration_sec'].append(build['duration_sec'])
        build_data['slave'].append(build['slave'])
    df = pd.DataFrame(build_data,
                      columns=['timestamp', 'success', 'failure', 'aborted',
                               'duration_sec', 'slave'])
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    df.index = df['timestamp']
    del df['timestamp']
    log.debug('df:\n%s\n', df)
    return df


def get_builds(args, data_file):
    # read from data_file if it exists, create new one if it doesn't
    builds = dict()

    first_run = False
    try:
        with open(data_file, 'r') as f:
            for line in f:
                build = json.loads(line)
                number = build['number']
                log.debug('Read existing record for build %s', number)
                builds[number] = build
    except (IOError, EOFError):
        first_run = True
        log.warn('%s not found, creating new data_file', data_file)

    log.info('Read %d builds from %s', len(builds), data_file)

    # If this is the first run for a job, pull all builds from Jenkins,
    # otherwise use the standard jenkins call which limits to 100 results
    # (the assumption is we're running regularly enough after the first run
    # that those 100 results is sufficient)
    if first_run:
        log.info('%s is a new job, querying all builds', args.jenkins_job)
        payload = {'tree': 'allBuilds[number]'}
        jenkins_url = '%s/job/%s/api/json' % (args.jenkins_server,
                                              args.jenkins_job)
    else:
        payload = {}
        jenkins_url = '%s/job/%s/api/json' % (args.jenkins_server,
                                              args.jenkins_job)

    log.debug('Retrieving jenkins data from %s', jenkins_url)
    session = requests.Session()
    r = session.get(jenkins_url, params=payload,
                    verify=args.verify_https_requests)
    if not r.ok:
        log.critical("Failed to fetch from %s", jenkins_url)
        exit(1)
    jenkins_data = r.json()
    new_builds = list()

    if first_run:
        jenkins_builds = jenkins_data['allBuilds']
    else:
        jenkins_builds = jenkins_data['builds']

    for build in jenkins_builds:
        number = build['number']
        if number in builds:
            log.debug('Already stored record for build %d', number)
            continue
        log.debug('Retrieving record for build %d', number)
        jenkins_url = '%s/job/%s/%s/api/json' % (args.jenkins_server,
                                                 args.jenkins_job, number)
        r = session.get(jenkins_url, verify=args.verify_https_requests)
        build_data = r.json()
        result = build_data['result']
        duration = build_data['duration']
        if result is None or duration == 0:
            log.debug(
                'Skipping unfinished build %s (result = %s, duration = %d)',
                number, result, duration)
            continue

        branch = None
        change_id = None
        change_url = None
        queue = None

        # Settings used only by OpenStack Zuul
        for param in build_data['actions'][1].get('parameters', list()):
            if param['name'] == 'ZUUL_BRANCH':
                branch = param['value']
            if param['name'] == 'ZUUL_CHANGE':
                change_id = param['value']
            if param['name'] == 'ZUUL_PROJECT':
                project = param['value']
            if param['name'] == 'ZUUL_PIPELINE':
                queue = param['value']

        # TODO reimplement to make this useful outside of Zuul
        if not queue:
            log.warn('Skipping build %s with null fields '
                     '(possibly manually triggered)', number)
            continue
        timestamp = int(build_data['timestamp'])
        build_time = time.strftime('%Y-%m-%d %H:%M:%S',
                                   time.localtime(timestamp / 1000))
        build_duration_sec = int(build_data['duration'] / 1000)
        end_timestamp = timestamp + int(build_data['duration'])
        build_end_time = time.strftime('%Y-%m-%d %H:%M:%S',
                                       time.localtime(end_timestamp / 1000))

        # TODO remove hard-coding of url
        if change_id is not None:
            change_url = 'https://review.openstack.org/%s' % change_id
        build_slave = build_data['builtOn']
        build = {'number': number,
                 'project': project,
                 'branch': branch,
                 'change_id': change_id,
                 'change_url': change_url,
                 'result': result,
                 'start_time': build_time,
                 'end_time': build_end_time,
                 'duration_sec': build_duration_sec,
                 'slave': build_slave,
                 'queue': queue
                 }
        builds[number] = build
        new_builds.append(build)
    if len(new_builds) > 0:
        with open(data_file, 'a') as f:
            for build in new_builds:
                log.info('Storing new record for build %s', build['number'])
                json.dump(build, f, separators=(',', ':'))
                f.write('\n')
            log.debug('Wrote %d new results to data_file %s',
                      len(new_builds), data_file)
    else:
        log.info('No new jobs to write to %s', data_file)

    return builds


def plot_status(df):
    plot_title = 'Success/Failure rates'

    # colours from http://clrs.cc/
    success = go.Bar(
        x=df.index,
        y=df['success_pct'],
        name='% Success',
        marker=dict(
            color='#2ca02c'
        )
    )
    failure = go.Bar(
        x=df.index,
        y=df['failure_pct'],
        name='% Failure',
        marker=dict(
            color='#de3c0c'
        )
    )
    jobs = go.Scatter(
        x=df.index,
        y=df['total'],
        name='Jobs',
        yaxis='y2',
        marker = dict(
            color='#1b689d'
        )
    )
    data = [success, failure, jobs]

    layout = go.Layout(
        barmode='stack',
        title=plot_title,
        xaxis=dict(tickformat="%d-%b-%Y", tickmode="linear"),
        yaxis=dict(ticksuffix="%", range=[0, 100]),
        yaxis2=dict(title="Jobs", overlaying='y', side='right', range=[0, 200]),
        legend=dict(orientation="h", x=0.02, y=1.15)
    )
    fig = go.Figure(data=data, layout=layout)
    plot = plotly.offline.plot(fig,
                               show_link=False,
                               output_type='div',
                               include_plotlyjs='False')
    return plot


def plot_slave_status(stats):
    plot_title = 'Per-slave Failure rates'

    # http://www.colorfavs.com/colors/f21b25/
    shades_of_red = ['#160203', '#580a0d', '#9a1118', '#dc1922', '#fbc1c4',
                     '#f88388', '#f4444d']
    data = []
    for slave in stats:
        failure = go.Bar(
            x=stats[slave].index,
            y=stats[slave]['failure_pct'],
            name=slave,
            text=[('%d total jobs' % x) for x in stats[slave]['total']],
            marker=dict(
                color=shades_of_red.pop()
            )
        )
        data.append(failure)

    layout = go.Layout(
        barmode='group',
        title=plot_title,
        xaxis=dict(tickformat="%d-%b-%Y", tickmode="linear"),
        yaxis=dict(ticksuffix="%", range=[0, 100]),
        legend=dict(orientation="h", x=0.02, y=1.15)
    )
    fig = go.Figure(data=data, layout=layout)
    plot = plotly.offline.plot(fig,
                               show_link=False,
                               output_type='div',
                               include_plotlyjs='False')
    return plot


def plot_duration(df):
    plot_title = 'Duration for successful jobs'

    percentile90 = go.Scatter(
            x=df.index,
            y=df['success_duration_min_90th'],
            mode='lines+markers',
            name='Duration (90th percentile)')
    percentile50 = go.Scatter(
            x=df.index,
            y=df['success_duration_min_50th'],
            mode='lines+markers',
            name='Duration (50th percentile)')

    data = [percentile50, percentile90]
    layout = go.Layout(
        title=plot_title,
        xaxis=dict(tickformat="%d-%b-%Y", tickmode="linear"),
        yaxis=dict(ticksuffix=" min", range=[0, 40]),
        legend=dict(orientation="h", x=0.02, y=1.15)
    )
    fig = go.Figure(data=data, layout=layout)
    plot = plotly.offline.plot(fig,
                               show_link=False,
                               output_type='div',
                               include_plotlyjs='False')
    return plot


def plot_slave_duration(stats):
    plot_title = 'Per-slave Duration (90th percentile)'

    # http://www.colorfavs.com/colors/318efb/
    shades_of_blue = ['#040d17', '#12345b', '#1f5aa0', '#2d81e4', '#69adfc',
                      '#a1ccfd', '#daeafe']
    data = []
    for slave in stats:
        failure = go.Bar(
            x=stats[slave].index,
            y=stats[slave]['success_duration_min_90th'],
            name=slave,
            marker=dict(
                color=shades_of_blue.pop()
            )
        )
        data.append(failure)

    layout = go.Layout(
        barmode='group',
        title=plot_title,
        xaxis=dict(tickformat="%d-%b-%Y", tickmode="linear"),
        yaxis=dict(ticksuffix=" min", range=[0, 40]),
        legend=dict(orientation="h", x=0.02, y=1.15)
    )
    fig = go.Figure(data=data, layout=layout)
    plot = plotly.offline.plot(fig,
                               show_link=False,
                               output_type='div',
                               include_plotlyjs='False')
    return plot


def configure_logging(args):
    """Configure logging.

    - Default => INFO
    - log_quietly (-q) => ERROR
    - log_verbosely (-v) => DEBUG
    """

    # requests and urllib3 are very chatty by default, suppress some of this
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)

    # and suppress InsecurePlatformWarning from urllib3 also
    # see http://stackoverflow.com/questions/29099404 for details
    import requests.packages.urllib3
    requests.packages.urllib3.disable_warnings()

    # or maybe https://stackoverflow.com/a/28002687
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    # Set root logger level to DEBUG, and use the
    # handler level to control verbosity.
    logging.getLogger().setLevel(logging.DEBUG)

    ch = logging.StreamHandler(stream=sys.stderr)
    if args.log_quietly:
        ch.setLevel(logging.ERROR)
    elif args.log_verbosely:
        ch.setLevel(logging.DEBUG)
    else:
        ch.setLevel(logging.INFO)

    ch_format = logging.Formatter('%(message)s')
    ch.setFormatter(ch_format)
    logging.getLogger().addHandler(ch)

    if not args.no_logfile:
        fh = logging.FileHandler(args.logfile, delay=True)
        # if args.log_verbosely:
        #     fh.setLevel(logging.DEBUG)
        # else:
        #     fh.setLevel(logging.INFO)
        fh.setLevel(logging.DEBUG)
        log_format = (
            '%(asctime)s: %(process)d:%(thread)d %(levelname)s - %(message)s'
        )
        fh_format = logging.Formatter(log_format)
        fh.setFormatter(fh_format)
        logging.getLogger().addHandler(fh)


if __name__ == "__main__":
    main()
