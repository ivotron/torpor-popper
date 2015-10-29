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
                    help='Whether to tune or generate target results')
parser.add_argument('--categories', default=['processor', 'memory'], nargs='+',
                    help=('Type of benchmarks to consider. One or more values'
                          ' of processor, memory, io or net.'))
parser.add_argument('--target-file', default='target.json',
                    help=('JSON file containing target performance results'))
parser.add_argument('--output-file', default='parameters.json',
                    help=('output JSON file containing resulting parameters'))
parser.add_argument('--benchmarks', default=['stream-copy', 'crafty'],
                    nargs='+', help='benchmarks to execute')
parser.add_argument('--show-bench-results', action='store_true',
                    help=('Show result of each benchmark (for every test)'))
# internal arguments
parser.add_argument('--category', help=argparse.SUPPRESS)
parser.add_argument('--target', type=json.loads, help=argparse.SUPPRESS)
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


class PortaTuner(MeasurementInterface):
    def manipulator(self):
        """
        Define the search space by creating a
        ConfigurationManipulator
        """
        manipulator = ConfigurationManipulator()

        if args.category == 'processor':
            manipulator.add_parameter(
                IntegerParameter('cpu-quota', 5000, 100000))
        elif args.category == 'memory':
            manipulator.add_parameter(
                IntegerParameter('mem-bw-limit', 10, 350))
        else:
            raise Exception('Unknown benchmark class ' + args.category)

        return manipulator

    def run(self, desired_result, input, limit):
        """
        Run a given configuration and return accuracy
        """
        cfg = desired_result.configuration.data

        target = self.args.target
        benchs = self.get_benchmarks_for_category(self.args.benchmarks, target,
                                                  self.args.category)
        if not benchs:
            raise Exception("No benchmarks for " + self.args.category)

        docker_cmd = self.get_cmd_for_class(self.args.category, benchs, cfg)

        result = self.call_program(docker_cmd)
        if result['returncode'] != 0:
            raise Exception(
                'Non-zero exit code:\n{}\nstdout:\n{}\nstderr:\n{}'.format(
                    docker_cmd, str(result['stdout']), str(result['stderr'])))

        current = json.loads(result['stdout'])

        diff_count = 0
        diff_sum = 0.0
        for bench in current:
            if self.args.show_bench_results:
                log.info(
                    bench + ": " + current[bench]['result'] + " " + str(cfg))
            current_result = float(current[bench]['result'])
            target_result = float(target[bench]['result'])
            diff_sum += abs(current_result - target_result)
            diff_count += 1
        diff_mean = diff_sum / diff_count
        diff_mean += 1  # avoid division by zero

        # TODO: multiply accuracy by variance (or stddev) to factor in the
        # variability in the differences between distinct results

        return Result(accuracy=(1000/diff_mean), time=0.0)

    def objective(self):
        return opentuner.search.objective.MaximizeAccuracy()

    def get_benchmarks_for_category(self, benchmarks, target, category):
        benchs = []
        for benchmark in target:
            if benchmark in benchmarks:
                if target[benchmark]['class'] == category:
                    benchs.append(benchmark)
        return benchs

    def get_cmd_for_class(self, category, benchmarks, cfg):
        if category == 'processor':
            return ('docker run '
                    ' --cpuset-cpus=0'
                    ' --cpu-quota={}'
                    ' -e BENCHMARKS="{}"'
                    ' --rm'
                    ' ivotron/microbench').format(
                        cfg['cpu-quota'],
                        ' '.join(benchmarks))
        elif category == 'memory':
            return ('docker-run-wrapper {}'
                    ' --cpuset-cpus=0'
                    ' -e BENCHMARKS="{}"'
                    ' --rm'
                    ' ivotron/microbench').format(
                        cfg['mem-bw-limit'],
                        ' '.join(benchmarks))

    def save_final_config(self, configuration):
        '''
        called at the end with best resultsdb.models.Configuration
        '''
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
        if not args.target_file:
            raise Exception('Expecting name of file with target results')
        with open(args.target_file) as f:
            args.target = json.load(f)

        # initialize output dict
        args.outjson = {}

        # invoke opentuner for each category
        for category in args.categories:
            args.category = category
            PortaTuner.main(args)

        # write output file
        with open(args.output_file, 'a') as f:
            json.dump(args.outjson, f)