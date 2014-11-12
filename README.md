pydpiper is a set of python modules that offers programmatic control over pipelines. 

It is very much under active development. 

Classes for building and controling the pipeline can be found in pydpiper/pipeline.py. 
The location of the pipeline directory must be in the users PYTHONPATH. 

The code for executing the pipeline can be found in pipeline_executor.py.

sge_batch is a perl script (written independently of this code) that is required to use the --queue=sge option currently written into pydpiper. It may be rewritten or moved to a separate subdirectory in the future. For now, this script should reside in a directory in the user's path. 

You can use environment variables to override our configuration defaults for the underlying Pyro library, except for
$PYRO_SERVERTYPE and $PYRO_LOGFILE; in particular, you may wish to change $PYRO_LOGLEVEL, since this also controls
the verbosity of some of the application's own logging.  See the Pyro4 documentation for more options.

Application modules that utilize the pipeline class definitions are currently in applications folder. These applications may be moved to a separate repository at a later date. 
 