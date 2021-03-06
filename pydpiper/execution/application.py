
from collections import defaultdict
import pkg_resources
import logging
import networkx as nx
import os
import sys
import shutil
import time

from typing import NamedTuple, List, Callable, Any

from pydpiper.core.stages import Result
from pydpiper.core.arguments import (CompoundParser, AnnotatedParser, application_parser,
                                     registration_parser, execution_parser, parse)
from pydpiper.execution.pipeline import Pipeline, pipelineDaemon
from pydpiper.execution.queueing import runOnQueueingSystem
from pydpiper.execution.pipeline_executor import ensure_exec_specified
from pydpiper.core.util import output_directories
from pydpiper.core.conversion import convertCmdStage

PYDPIPER_VERSION = pkg_resources.get_distribution("pydpiper").version  # pylint: disable=E1101

logger = logging.getLogger(__name__)

ExecutionConf = NamedTuple('ExecutionConf', [('use_backup_files', bool),
                                             ('create_graph', bool),
                                             ('execute', bool)])
# TODO: put remainder of executor args here?


def output_dir(options):
    return options.application.output_directory if options.application.output_directory else os.getcwd()

def write_stages(stages, name):
    """
    writes all pipeline stages to a file
    """
    with open(os.path.join(os.curdir, "%s_pipeline_stages.txt" % name), 'w') as pf:
        for i, stage in enumerate(stages):
            # TODO indices of this enumeration only correspond to stage numbers by "coincidence"
            # (a similar iteration is performed elsewhere with the same results) but this is sort of silly/dangerous
            pf.write(str(i) + "\t" + str(stage.render()) + "\n")

def file_graph(stages, pipeline_dir):
    # TODO remove pipeline_dir from node pathnames
    G = nx.DiGraph()
    #files = [f.path for s in stages for f in s.inputs]
    #G.add_nodes_from(files)
    # could filter out boring outputs here...
    for s in stages:
        for x in s.inputs:
            for y in s.outputs:
                G.add_edge(os.path.relpath(x.path, pipeline_dir),
                           os.path.relpath(y.path, pipeline_dir),
                           label=s.to_array()[0])
    return G
    # TODO: nx.write_dot doesn't show `cmd` attribute;
    # need to use something like nx.to_pydot to convert


def ensure_short_output_paths(stages, max_len=255):  # magic no. for EXT3, EXT4, NFS (?), Linux NAME_MAX, etc.
    # N.B. - at some point we had 245 instead of 255 -- a typo, a program-specific buffer size,
    # or something to do with one of the file systems (NFS, SciNet's IBM GPFS, etc. ...)?
    # TODO check the other parts of the path aren't too long either (much less likely)?
    for s in stages:
        for o in [o.filename_wo_ext for o in s.outputs] + [os.path.basename(s.log_file)]:
            if len(o) > max_len:
                raise ValueError("output filename '%s' too long (more than %s chars)" % (o, max_len))


def ensure_output_paths_in_dir(stages, d):
    # TODO also check the logfiles ... should these be counted as stage outputs (tedious to add by hand ...)?
    for s in stages:
        for o in s.outputs:
            if os.path.relpath(o.path, d).startswith('..'):
                raise ValueError("output %s of stage %s not contained inside "
                                 "pipeline directory %s" % (o.path, s, d))


# TODO: where should this live - util?
# TODO: write some tests to check that this might be working
# TODO: could generalize to '(non)distinctOn' but nobody would ever use ...
def nondistinct_outputs(stages):
    """
    TODO: move this doctest to a proper test in test/
    >>> c1 = CmdStage(argArray=["touch", OutputFile("/tmp/foo.txt")])
    >>> c2 = CmdStage(argArray=[">",     OutputFile("/tmp/foo.txt")])
    >>> nondistinct_outputs([c1, c2]) == { '/tmp/foo.txt' : set([c1,c2]) }
    True
    """
    m = ((o, s) for s in stages for o in s.outputFiles)
    d = defaultdict(set)
    for o, s in m:
        d[o].add(s)
    bad_outputs = { o : ss for o, ss in d.items() if len(ss) > 1 }
    return bad_outputs


def ensure_distinct_outputs(stages):
    # TODO logfiles as well? (see comment in `ensure_output_paths_in_dir`)
    bad_outputs = nondistinct_outputs(stages)
    if len(bad_outputs) >= 1:
        print("Uh-oh - some files appear as outputs of multiple stages, to wit:", file=sys.stderr)
        for o, ss in bad_outputs.items():
            print("output: %s\nstages:\n" % o, file=sys.stderr)
            for s in ss:
                print("%s\n" % s, file=sys.stderr)
            print("\n", file=sys.stderr)
        raise ValueError("Conflicting outputs:", bad_outputs)


def ensure_commands_exist(stages):
    cmds = set((s.to_array()[0] for s in stages))
    bad_cmds = [cmd for cmd in cmds if shutil.which(cmd) is None]
    if len(bad_cmds) > 0:
        raise ValueError("Missing executables: %s" % bad_cmds)


#TODO: change this to ...(static_pipeline, options)?
def execute(stages, options):
    """Basically just looks at the arguments and exits if `--no-execute` is specified,
    otherwise dispatches on backend type."""

    # TODO: logger.info('Constructing pipeline...')
    pipeline = Pipeline(stages=[convertCmdStage(s) for s in stages],
                        options=options)

    # TODO: print/log version
    reconstruct_command(options)

    write_stages(stages, options.application.pipeline_name)

    if options.application.create_graph:
        # TODO: these could have more descriptive names ...
        logger.debug("Writing dot file...")
        nx.drawing.nx_agraph.write_dot(pipeline.G, str(options.application.pipeline_name) + "_labeled-tree.dot")
        nx.drawing.nx_agraph.write_dot(file_graph(stages, options.application.output_directory),
                                       str(options.application.pipeline_name) + "_labeled-tree-alternate.dot")
        logger.debug("Done.")

    # for debugging reasons, it's best if these come after writing stages, drawing graph, ...
    ensure_short_output_paths(stages)  # TODO convert to new `CmdStage`s
    ensure_output_paths_in_dir(stages, options.application.output_directory)
    ensure_distinct_outputs([convertCmdStage(s) for s in stages])
    ensure_commands_exist(stages)

    if not options.application.execute:
        print("Not executing the command (--no-execute is specified).\nDone.")
        return

    # TODO: why is this needed now that --version is also handled automatically?
    # --num-executors=0 (<=> --no-execute) could be the default, and you could
    # also refuse to submit scripts for 0 executors ...
    ensure_exec_specified(options.execution.num_exec)

    # TODO: move calls to create_directories into execution functions
    #create_directories(stages)

    execution_proc = backend(options)
    execution_proc(pipeline, options)


def mk_application(parsers: List[AnnotatedParser], pipeline: Callable[[Any], Result[Any]]) -> Callable[[], Any]:
    """Wire up a pure-python pipeline application into a command-line application."""
    # TODO the type isn't very precise ...
    p = CompoundParser([application_parser,
                        registration_parser,
                        execution_parser
                        ] + parsers)
    def f():
        options = parse(p, sys.argv[1:])
        execute(pipeline(options).stages, options)
    return f


def backend(options):
    return grid_only_execute if options.execution.submit_server else normal_execute

# TODO: should create_directories be added as a method to Pipeline?
def create_directories(stages):
    dirs = output_directories(stages)
    for d in dirs:
        # some files that are created by the pipeline end up in the
        # output directory which can be ''. In that case, one of the
        # directories returned in dirs is in fact '', but we should
        # not try to create it...
        if d != '':
            os.makedirs(d, exist_ok=True)

# The old AbstractApplication class has been removed due to its non-obvious API.  In its place,
# we currently provide an `execute` function and some helper functions for command-line parsing, as well
# as a `mk_application` function which inverts control again, although with a clearer interface (hopefully)
# than AbstractApplication.

def normal_execute(pipeline, options):
    # FIXME this is a trivial function; inline pipelineDaemon here
    #pipelineDaemon runs pipeline, launches Pyro client/server and executors (if specified)
    logger.info("Starting pipeline daemon...")
    # TODO: make a flag to disable this in case already created, wish to create later, etc.
    create_directories(pipeline.stages) # TODO: or whatever
    pipelineDaemon(pipeline, options, sys.argv[0])
    logger.info("Server has stopped.  Quitting...")

def grid_only_execute(pipeline, options):
    if options.execution.queue_type is not 'pbs':
        raise ValueError("currently we only support submitting the server to PBS/Torque systems")
    roq = runOnQueueingSystem(options, sys.argv)
    roq.createAndSubmitPbsScripts()
    # TODO: make the local server create the directories (first time only) OR create them before submitting OR submit a separate stage?
    # NOTE we can't add a stage to the pipeline at this point since the pipeline doesn't support any sort of incremental recomputation ...
    logger.info("Finished submitting PBS job scripts...quitting")

def reconstruct_command(options):
    # TODO: also write down the environment, contents of config files
    reconstruct = ' '.join(sys.argv)
    logger.info("Command is: " + reconstruct)
    logger.info("Command version : " + PYDPIPER_VERSION)
    fileForCommandAndVersion = options.application.pipeline_name + "-command-and-version-" + time.strftime("%d-%m-%Y-at-%H-%m-%S") + ".sh"
    pf = open(fileForCommandAndVersion, "w")
    pf.write("#!/usr/bin/env bash\n")
    for p in ["PATH", "PYTHONPATH", "LD_LIBRARY_PATH", "PERL5LIB"]:
        pf.write("#export %s=%s\n" % (p, os.getenv(p)))
    pf.write("# Command version is: " + PYDPIPER_VERSION + "\n")
    pf.write("# Command was: \n")
    pf.write(reconstruct + '\n')
    pf.write("# options were: \n# %s" % options)
    pf.close()
 
