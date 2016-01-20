#!/usr/bin/env python
#
# Autotune --cpu-quota and --mem-bw-limit parameters to port the performance of
# a container between machines

import os
t = os.path.join(os.path.dirname(__file__), os.path.expandvars(
                 '$OPENTUNER_DIR/opentuner/utils/adddeps.py'))
execfile(t, dict(__file__=t))

import argparse
import json
import subprocess
import logging
import opentuner
from opentuner import ConfigurationManipulator
from opentuner import IntegerParameter
from opentuner import MeasurementInterface
from opentuner import Result
from opentuner.search import technique

log = logging.getLogger(__name__)

parser = argparse.ArgumentParser(parents=opentuner.argparsers())
parser.add_argument('--action', default='none',
                    choices=('base', 'tune'),
                    help='Whether to tune or generate base results')
parser.add_argument('--categories', default=['processor', 'memory'], nargs='+',
                    help=('Type of benchmarks to consider. One or more values'
                          ' of processor, memory, io or net.'))
parser.add_argument('--base-file', default='base.json',
                    help=('JSON file containing results of base system'))
parser.add_argument('--output-file', default='parameters.json',
                    help=('output JSON file containing resulting parameters'))
parser.add_argument('--benchmarks',
                    default=['stream-copy', 'stream-add', 'stream-scale',
                             'stream-triad', 'crafty', 'c-ray'],
                    nargs='+', help='benchmarks to execute')
parser.add_argument('--show-bench-results', action='store_true',
                    help=('Show result of each benchmark (for every test)'))
parser.add_argument('--cpuquota', default='',
                    help="For other-than-cpu categories, this must be given")
# internal arguments
parser.add_argument('--category', help=argparse.SUPPRESS)
parser.add_argument('--base-data', type=json.loads, help=argparse.SUPPRESS)
parser.add_argument('--outjson', help=argparse.SUPPRESS)


class MonotonicSearch(technique.SequentialSearchTechnique):
    """
    Assumes a monotonically increasing/decreasing function
    """
    def main_generator(self):
        objective = self.objective
        driver = self.driver
        manipulator = self.manipulator

        n = driver.get_configuration(manipulator.random())
        current = manipulator.copy(n.data)

        # we only handle one parameter for now
        if len(manipulator.parameters(n.data)) > 1:
            raise Exception("Only one parameter for now")

        # start at the highest value for the parameter
        for param in manipulator.parameters(n.data):
            param.set_value(current, param.max_value)
        current = driver.get_configuration(current)
        yield current

        step_size = 0.15
        go_down = True
        n = current

        while True:
            for param in manipulator.parameters(current.data):
                # get current value of param, scaled to be in range [0.0, 1.0]
                unit_value = param.get_unit_value(current.data)

                if go_down:
                    n = manipulator.copy(current.data)
                    param.set_unit_value(n, max(0.0, unit_value - step_size))
                    n = driver.get_configuration(n)
                    yield n
                else:
                    n = manipulator.copy(current.data)
                    param.set_unit_value(n, max(0.0, unit_value + step_size))
                    n = driver.get_configuration(n)
                    yield n

            if objective.lt(n, current):
                # new point is better, so that's the new current
                current = n
            else:
                # if we were going down, then go up but half-step (or viceversa)
                go_down = not go_down
                step_size /= 2.0

# register our new technique in global list
technique.register(MonotonicSearch())


def get_result(results_data, bench_name):
    for bench in results_data:
        if bench['name'] == bench_name:
            return float(bench['result'])
    raise Exception("Can't find result for benchmark " + bench_name)


def get_benchmarks_for_category(benchmarks, base_data, category):
    benchs = []
    for bench in base_data:
        if bench['name'] in benchmarks and bench['class'] == category:
            benchs.append(bench['name'])
    return benchs


def get_cmd_for_class(category, benchmarks, cfg):
    if category == 'processor':
        return ('docker run '
                ' --cpuset-cpus=0'
                ' --cpu-quota={}'
                ' --cpu-period={}'
                ' -e BENCHMARKS="{}"'
                ' --rm'
                ' ivotron/microbench').format(
                    cfg['cpu-quota'], "50000",
                    ' '.join(benchmarks))
    elif category == 'memory':
        if args.cpuquota is None:
            raise Exception("Expecting value for cpuquota")

        return ('docker-run-wrapper {} {}'
                ' --cpuset-cpus=0'
                ' --cpu-quota={}'
                ' --cpu-period={}'
                ' -e BENCHMARKS="{}"'
                ' --rm'
                ' ivotron/microbench').format(
                    "1000", cfg['mem-bw-limit'], args.cpuquota, "50000",
                    ' '.join(benchmarks))


class PortaTuner(MeasurementInterface):
    def manipulator(self):
        """
        Define the search space by creating a
        ConfigurationManipulator
        """
        manipulator = ConfigurationManipulator()

        if args.category == 'processor':
            manipulator.add_parameter(
                IntegerParameter('cpu-quota', 5000, 50000))
        elif args.category == 'memory':
            manipulator.add_parameter(
                IntegerParameter('mem-bw-limit', 100, 2000))
        else:
            raise Exception('Unknown benchmark class ' + args.category)

        return manipulator

    def run(self, desired_result, input, limit):
        """
        Run a given configuration and return accuracy
        """
        cfg = desired_result.configuration.data

        base_data = self.args.base_data
        benchs = get_benchmarks_for_category(self.args.benchmarks,
                                             base_data,
                                             self.args.category)
        if not benchs:
            raise Exception("No benchmarks for " + self.args.category)

        docker_cmd = get_cmd_for_class(self.args.category, benchs, cfg)

        has_incomplete_results = True

        while has_incomplete_results:
            result = self.call_program(docker_cmd)
            if result['returncode'] != 0:
                raise Exception(
                    'Non-zero exit code:\n{}\nstdout:\n{}\nstderr:\n{}'.format(
                        docker_cmd, str(result['stdout']),
                        str(result['stderr'])))

            target_data = json.loads(result['stdout'])

            # check that we got results for all benchmarks, otherwise re-run
            try:
                for bench_name in benchs:
                    get_result(target_data, bench_name)
            except Exception, e:
                if "Can't find result for benchmark" in str(e):
                    raise
                # let's try again
                continue

            has_incomplete_results = False

        count = 0
        speedup_sum = 0.0
        for bench_name in benchs:
            base_result = get_result(base_data, bench_name)
            target_result = get_result(target_data, bench_name)
            speedup = target_result/base_result
            speedup_sum += speedup
            count += 1

            if self.args.show_bench_results:
                log.info(bench_name + ": " + str(target_result) + " " +
                         str(cfg) + " speedup: " + str(speedup))

        speedup_mean = speedup_sum / count

        if speedup_mean < 1.0:
            # heuristic that reflects the speedup on 1.0 to prevent the
            # optimization to minimize up to 0. E.g. instead of having
            # a speedup of 0.952, we have a speedup of 1.048
            speedup_mean = 1 + (1.0 - speedup_mean)

        return Result(time=speedup_mean)

    def save_final_config(self, configuration):
        '''
        called at the end with best resultsdb.models.Configuration
        '''
        if args.category == 'processor':
            args.cpuquota = configuration.data['cpu-quota']
        self.args.outjson.update(configuration.data)

if __name__ == '__main__':
    args = parser.parse_args()

    if args.action == 'none':
        raise Exception("Need one action provided with --action")
    elif args.action == 'base':
        print(subprocess.check_output(
              ('docker run --rm --cpuset-cpus=0 -e BENCHMARKS="{}"'
               '  ivotron/microbench').format(' '.join(args.benchmarks)),
              stderr=subprocess.PIPE, shell=True))
    elif args.action == 'tune':
        # read input
        if not args.base_file:
            raise Exception('Expecting name of file with base results')
        with open(args.base_file) as f:
            args.base_data = json.load(f)

        # initialize output dict
        args.outjson = {}

        # invoke opentuner for each category
        for category in args.categories:
            args.category = category
            PortaTuner.main(args)

        # write output file
        with open(args.output_file, 'w') as f:
            json.dump(args.outjson, f)
            f.write('\n')
