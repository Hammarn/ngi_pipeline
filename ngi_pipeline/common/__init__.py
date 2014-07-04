#!/usr/bin/env python
"""
This script organizes demultiplexed (CASAVA 1.8) sequencing data into the relevant
project/sample/flowcell directory structure.
"""

## For testing, use the top directory /pica/v3/a2010002/archive/131030_SN7001362_0103_BC2PUYACXX

from __future__ import print_function

import argparse
import collections
import fnmatch
import glob
import importlib
import os
import re
import subprocess
import sys
import unittest
import yaml

from ngi_pipeline.common.parsers import FlowcellRunMetricsParser
from ngi_pipeline.log import minimal_logger
from ngi_pipeline.utils import memoized, safe_makedir
from ngi_pipeline.utils.config import load_yaml_config

LOG = minimal_logger(__name__)


def main(demux_fcid_dirs, config_file_path=None, restrict_to_projects=None, restrict_to_samples=None):
    """
    The main launcher method.

    :param list demux_fcid_dirs: The CASAVA-produced demux directory/directories.
    :param str config_file_path: The path to the configuration file; can also be
                                 specified via environmental variable "NGI_CONFIG"
    :param list restrict_to_projects: A list of projects; analysis will be
                                      restricted to these. Optional.
    :param list restrict_to_samples: A list of samples; analysis will be
                                     restricted to these. Optional.
    """
    if not config_file_path:
        config_file_path = os.environ.get("NGI_CONFIG") or os.path.expandvars(os.path.join("$HOME/.ngipipeline/ngi_config.yaml"))
        if not os.path.isfile(config_file_path):
            error_msg = ("Configuration file \"{}\" does not exist or is not a "
                         "file. Cannot proceed.".format(config_file_path))
            LOG.error(error_msg)
            raise RuntimeError(error_msg)
    if not restrict_to_projects: restrict_to_projects = []
    if not restrict_to_samples: restrict_to_samples = []
    demux_fcid_dirs_set = set(demux_fcid_dirs)
    projects_to_analyze = []
    config = load_yaml_config(config_file_path)

    # Sort/copy each raw demux FC into project/sample/fcid format -- "analysis-ready"
    projects_to_analyze = dict()
    for demux_fcid_dir in demux_fcid_dirs_set:
        # These will be a bunch of Project objects each containing Samples, FCIDs, lists of fastq files
        projects_to_analyze = setup_analysis_directory_structure(demux_fcid_dir,
                                                                 config,
                                                                 projects_to_analyze,
                                                                 restrict_to_projects,
                                                                 restrict_to_samples)
    if not projects_to_analyze:
        error_message = "No projects found to process."
        LOG.info(error_message)
        sys.exit("Quitting: " + error_message)
    else:
        # Don't need the dict functionality anymore; revert to list
        projects_to_analyze = projects_to_analyze.values()

    analysis_engine_module_name = config.get("analysis", {}).get("analysis_engine")
    if not analysis_engine_module_name:
        error_msg = "No analysis engine specified in configuration file. Exiting."
        LOG.error(error_msg)
        raise RuntimeError(error_msg)
    # Import the module specified in the config file (e.g. bcbio, piper)
    analysis_module = importlib.import_module(analysis_engine_module_name)
    analysis_module.main(projects_to_analyze=projects_to_analyze, config_file_path=config_file_path)


def setup_analysis_directory_structure(fc_dir, config, projects_to_analyze,
                                       restrict_to_projects=None, restrict_to_samples=None):
    """
    Copy and sort files from their CASAVA-demultiplexed flowcell structure
    into their respective project/sample/FCIDs. This collects samples
    split across multiple flowcells.

    :param str fc_dir: The directory created by CASAVA for this flowcell.
    :param dict config: The parsed configuration file.
    :param set projects_to_analyze: A dict (of Project objects, or empty)
    :param list restrict_to_projects: Specific projects within the flowcell to process exclusively
    :param list restrict_to_samples: Specific samples within the flowcell to process exclusively

    :returns: A list of NGIProject objects that need to be run through the analysis pipeline
    :rtype: list

    :raises OSError: If the analysis destination directory does not exist or if there are permissions errors.
    :raises KeyError: If a required configuration key is not available.
    """
    LOG.info("Setting up analysis for demultiplexed data in source folder \"{}\"".format(fc_dir))
    if not restrict_to_projects: restrict_to_projects = []
    if not restrict_to_samples: restrict_to_samples = []
    analysis_top_dir = os.path.abspath(config["analysis"]["top_dir"])
    if not os.path.exists(analysis_top_dir):
        error_msg = "Error: Analysis top directory {} does not exist".format(analysis_top_dir)
        LOG.error(error_msg)
        raise OSError(error_msg)
    if not os.path.exists(fc_dir):
        LOG.error("Error: Flowcell directory {} does not exist".format(fc_dir))
        return []
    # Map the directory structure for this flowcell
    try:
        fc_dir_structure = parse_casava_directory(fc_dir)
    except RuntimeError as e:
        LOG.error("Error when processing flowcell dir \"{}\": {}".format(fc_dir, e))
        return []
    fc_date = fc_dir_structure['fc_date']
    fc_name = fc_dir_structure['fc_name']
    fc_run_id = "{}_{}".format(fc_date, fc_name)
    # Copy the basecall stats directory.
    # This will be causing an issue when multiple directories are present...
    # syncing should be done from archive, preserving the Unaligned* structures
    LOG.info("Copying basecall stats for run {}".format(fc_dir))
    _copy_basecall_stats([os.path.join(fc_dir_structure['fc_dir'], d) for d in
                                        fc_dir_structure['basecall_stats_dir']],
                                        analysis_top_dir)
    if not fc_dir_structure.get('projects'):
        LOG.warn("No projects found in specified flowcell directory \"{}\"".format(fc_dir))
    # Iterate over the projects in the flowcell directory
    for project in fc_dir_structure.get('projects', []):
        project_name = project['project_name']
        # If specific projects are specified, skip those that do not match
        if restrict_to_projects and project_name not in restrict_to_projects:
            LOG.debug("Skipping project {}".format(project_name))
            continue
        LOG.info("Setting up project {}".format(project.get("project_dir")))
        # Create a project directory if it doesn't already exist
        project_dir = os.path.join(analysis_top_dir, project_name)
        if not os.path.exists(project_dir): safe_makedir(project_dir, 0770)
        try:
            project_obj = projects_to_analyze[project_dir]
        except KeyError:
            project_obj = NGIProject(name=project_name, dirname=project_name, base_path=analysis_top_dir)
            projects_to_analyze[project_dir] = project_obj
        # Iterate over the samples in the project
        for sample in project.get('samples', []):
            # If specific samples are specified, skip those that do not match
            sample_name = sample['sample_name'].replace('__','.')
            if restrict_to_samples and sample_name not in restrict_to_samples:
                LOG.debug("Skipping sample {}".format(sample_name))
                continue
            LOG.info("Setting up sample {}".format(sample_name))
            # Create a directory for the sample if it doesn't already exist
            sample_dir = os.path.join(project_dir, sample_name)
            if not os.path.exists(sample_dir): safe_makedir(sample_dir, 0770)
            # This will only create a new sample object if it doesn't already exist in the project
            sample_obj = project_obj.add_sample(name=sample_name, dirname=sample_name)
            # Create a directory for the flowcell if it does not exist
            dst_sample_fcid_dir = os.path.join(sample_dir, fc_run_id)
            if not os.path.exists(dst_sample_fcid_dir): safe_makedir(dst_sample_fcid_dir, 0770)
            # This will only create a new FCID object if it doesn't already exist in the sample
            fcid_obj = sample_obj.add_fcid(name=fc_run_id, dirname=fc_run_id)
            # rsync the source files to the sample directory
            #    src: flowcell/data/project/sample
            #    dst: project/sample/flowcell_run
            src_sample_dir = os.path.join(fc_dir_structure['fc_dir'],
                                          project['data_dir'],
                                          project['project_dir'],
                                          sample['sample_dir'])
            LOG.info("Copying sample files from \"{}\" to \"{}\"...".format(
                                            src_sample_dir, dst_sample_fcid_dir))
            sample_files = do_rsync([os.path.join(src_sample_dir, f) for f in
                                     sample.get('files', [])], dst_sample_fcid_dir)
            # Just want fastq files here
            pattern = re.compile(".*\.(fastq|fq)(\.gz|\.gzip|\.bz2)?$")
            fastq_files = filter(pattern.match, sample.get('files', []))
            fcid_obj.add_fastq_files(fastq_files)
    return projects_to_analyze


def parse_casava_directory(fc_dir):
    """
    Traverse a CASAVA-1.8-generated directory structure and return a dictionary
    of the elements it contains.
    The flowcell directory tree has (roughly) the structure:

    |-- Data
    |   |-- Intensities
    |       |-- BaseCalls
    |-- InterOp
    |-- Unaligned
    |   |-- Basecall_Stats_C2PUYACXX
    |-- Unaligned_16bp
        |-- Basecall_Stats_C2PUYACXX
        |   |-- css
        |   |-- Matrix
        |   |-- Phasing
        |   |-- Plots
        |   |-- SignalMeans
        |   |-- Temp
        |-- Project_J__Bjorkegren_13_02
        |   |-- Sample_P680_356F_dual56
        |   |   |-- <fastq files are here>
        |   |   |-- <SampleSheet.csv is here>
        |   |-- Sample_P680_360F_dual60
        |   |   ...
        |-- Undetermined_indices
            |-- Sample_lane1
            |   ...
            |-- Sample_lane8

    :param str fc_dir: The directory created by CASAVA for this flowcell.

    :returns: A dict of information about the flowcell, including project/sample info
    :rtype: dict

    :raises RuntimeError: If the fc_dir does not exist or cannot be accessed,
                          or if Flowcell RunMetrics could not be parsed properly.
    """
    projects = []
    fc_dir = os.path.abspath(fc_dir)
    LOG.info("Parsing flowcell directory \"{}\"...".format(fc_dir))
    parser = FlowcellRunMetricsParser(fc_dir)
    run_info = parser.parseRunInfo()
    runparams = parser.parseRunParameters()
    try:
        fc_name = run_info['Flowcell']
        fc_date = run_info['Date']
        fc_pos = runparams['FCPosition']
    except KeyError as e:
        raise RuntimeError("Could not parse flowcell information {} "
                           "from Flowcell RunMetrics in flowcell {}".format(e, fc_dir))
    # "Unaligned*" because SciLifeLab dirs are called "Unaligned_Xbp"
    # (where "X" is the index length) and there is also an "Unaligned" folder
    unaligned_dir_pattern = os.path.join(fc_dir,"Unaligned*")
    basecall_stats_dir_pattern = os.path.join(unaligned_dir_pattern,"Basecall_Stats_*")
    basecall_stats_dir = [os.path.relpath(d,fc_dir) for d in glob.glob(basecall_stats_dir_pattern)]
    # e.g. 131030_SN7001362_0103_BC2PUYACXX/Unaligned_16bp/Project_J__Bjorkegren_13_02/
    project_dir_pattern = os.path.join(unaligned_dir_pattern,"Project_*")
    for project_dir in glob.glob(project_dir_pattern):
        LOG.info("Parsing project directory \"{}\"...".format(project_dir.split(os.path.split(fc_dir)[0] + "/")[1]))
        project_samples = []
        sample_dir_pattern = os.path.join(project_dir,"Sample_*")
        # e.g. <Project_dir>/Sample_P680_356F_dual56/
        for sample_dir in glob.glob(sample_dir_pattern):
            LOG.info("Parsing samples directory \"{}\"...".format(sample_dir.split(os.path.split(fc_dir)[0] + "/")[1]))
            fastq_file_pattern = os.path.join(sample_dir,"*.fastq.gz")
            samplesheet_pattern = os.path.join(sample_dir,"*.csv")
            fastq_files = [os.path.basename(file) for file in glob.glob(fastq_file_pattern)]
            samplesheet = glob.glob(samplesheet_pattern)
            assert len(samplesheet) == 1, \
                    "Error: could not unambiguously locate samplesheet in {}".format(sample_dir)
            sample_name = os.path.basename(sample_dir).replace("Sample_","").replace('__','.')
            project_samples.append({'sample_dir': os.path.basename(sample_dir),
                                    'sample_name': sample_name,
                                    'files': fastq_files,
                                    'samplesheet': os.path.basename(samplesheet[0])})
        project_name = os.path.basename(project_dir).replace("Project_","").replace('__','.')
        projects.append({'data_dir': os.path.relpath(os.path.dirname(project_dir),fc_dir),
                         'project_dir': os.path.basename(project_dir),
                         'project_name': project_name,
                         'samples': project_samples})
    return {'fc_dir': fc_dir,
            'fc_name': '{}{}'.format(fc_pos, fc_name),
            'fc_date': fc_date,
            'basecall_stats_dir': basecall_stats_dir,
            'projects': projects}


def _copy_basecall_stats(source_dirs, destination_dir):
    """Copy relevant files from the Basecall_Stats_FCID directory
       to the analysis directory
    """
    for source_dir in source_dirs:
        # First create the directory in the destination
        dirname = os.path.join(destination_dir,os.path.basename(source_dir))
        safe_makedir(dirname)
        # List the files/directories to copy
        files = glob.glob(os.path.join(source_dir,"*.htm"))
        files += glob.glob(os.path.join(source_dir,"*.metrics"))
        files += glob.glob(os.path.join(source_dir,"*.xml"))
        files += glob.glob(os.path.join(source_dir,"*.xsl"))
        for dir in ["Plots","css"]:
            d = os.path.join(source_dir,dir)
            if os.path.exists(d):
                files += [d]
        do_rsync(files,dirname)


# This isn't used at the moment
def copy_undetermined_index_files(casava_data_dir, destination_dir):
    """
    Copy fastq files with "Undetermined" index reads to the destination directory.
    :param str casava_data_dir: The Unaligned directory (e.g. "<FCID>/Unaligned_16bp")
    :param str destination_dir: Eponymous
    """
    # List of files to copy
    copy_list = []
    # List the directories containing the fastq files
    fastq_dir_pattern = os.path.join(casava_data_dir,"Undetermined_indices","Sample_lane*")
    # Pattern matching the fastq_files
    fastq_file_pattern = "*.fastq.gz"
    # Samplesheet name
    samplesheet_pattern = "SampleSheet.csv"
    samplesheets = []
    for dir in glob.glob(fastq_dir_pattern):
        copy_list += glob.glob(os.path.join(dir,fastq_file_pattern))
        samplesheet = os.path.join(dir,samplesheet_pattern)
        if os.path.exists(samplesheet):
            samplesheets.append(samplesheet)
    # Merge the samplesheets into one
    new_samplesheet = os.path.join(destination_dir,samplesheet_pattern)
    new_samplesheet = _merge_samplesheets(samplesheets,new_samplesheet)
    # Rsync the fastq files to the destination directory
    do_rsync(copy_list,destination_dir)

# Also not used at the moment
def _merge_samplesheets(samplesheets, merged_samplesheet):
    """
    Merge multiple Illumina SampleSheet.csv files into one.
    :param list samplesheets: A list of the paths to the SampleSheet.csv files to merge.
    :param str merge_samplesheet: The path <...>
    :returns: <...>
    :rtype: str
    """
    data = []
    header = []
    for samplesheet in samplesheets:
        with open(samplesheet) as fh:
            csvread = csv.DictReader(fh, dialect='excel')
            header = csvread.fieldnames
            for row in csvread:
                data.append(row)
    with open(merged_samplesheet, "w") as outh:
        csvwrite = csv.DictWriter(outh, header)
        csvwrite.writeheader()
        csvwrite.writerows(sorted(data, key=lambda d: (d['Lane'],d['Index'])))
    return merged_samplesheet


##  this could also work remotely of course
def do_rsync(src_files, dst_dir):
    ## TODO I changed this -c because it takes for goddamn ever but I'll set it back once in Production
    #cl = ["rsync", "-car"]
    cl = ["rsync", "-aPv"]
    cl.extend(src_files)
    cl.append(dst_dir)
    cl = map(str, cl)
    # For testing, just touch the files rather than copy them
    # for f in src_files:
    #    open(os.path.join(dst_dir,os.path.basename(f)),"w").close()
    subprocess.check_call(cl)
    return [ os.path.join(dst_dir,os.path.basename(f)) for f in src_files ]


def parse_lane_from_filename(sample_basename):
    """Project id, sample id, and lane are pulled from the standard filename format,
     which is:
       <lane_num>_<date>_<fcid>_<project>_<sample_num>_<read>.fastq[.gz]
       e.g.
       1_140220_AH8AMJADXX_P673_101_1.fastq.gz
       (SciLifeLab Sthlm format)
    or
       <sample-name>_<index>_<lane>_<read>_<group>.fastq.gz
       e.g.
       P567_102_AAAAAA_L001_R1_001.fastq.gz
       (Standard Illumina format)

    returns a tuple of (project_id, sample_id, lane) or raises a ValueError if there is no match
    (which shouldn't generally happen and probably indicates a larger issue).

    :param str sample_basename: The name of the file from which to pull the project id
    :returns: (project_id, sample_id)
    :rtype: tuple
    :raises ValueError: If the ids cannot be determined from the filename (no regex match)
    """
    ## TODO so it turns out Uppsala doesn't do this project_sample thing with their sample naming which is a little sad for me
    # Stockholm or Illumina
    match = re.match(r'(?P<lane>\d)_\d{6}_\w{10}_(?P<project>P\d{3})_(?P<sample>\d{3}).*', sample_basename) or \
            re.match(r'.*_L(?P<lane>\d{3}).*', sample_basename)
            #re.match(r'(?P<project>P\d{3})_(?P<sample>\w+)_.*_L(?P<lane>\d{3})', sample_basename)

    if match:
        #return match.group('project'), match.group('sample'), match.group('lane')
        return match.group('lane')
    else:
        error_msg = ("Error: filename didn't match conventions, "
                     "couldn't find project id for sample "
                     "\"{}\"".format(sample_basename))
        LOG.error(error_msg)
        raise ValueError(error_msg)


@memoized
def get_project_data_for_id(project_id, proj_db):
    """Pulls all the data about a project from the StatusDB
    given the project's id (e.g. "P602") and a couchdb view object.

    :param str project_id: The project ID
    :param proj_db: The project_db object
    :returns: A dict of the project data
    :rtype: dict
    """
    db_view = proj_db.view('project/project_id')
    try:
        return proj_db.get([proj.id for proj in db_view if proj.key == project_id][0])
    except IndexError:
        error_msg = "Warning: project ID '{}' not found in Status DB".format(project_id)
        LOG.error(error_msg)
        raise ValueError(error_msg)


def find_fastq_read_pairs(file_list=None, directory=None):
    """
    Given a list of file names, finds read pairs (based on _R1_/_R2_ file naming)
    and returns a dict of {base_name: [ file_read_one, file_read_two ]}
    Filters out files not ending with .fastq[.gz|.gzip|.bz2].
    E.g.
        1_131129_BH7VPTADXX_P602_101_1.fastq.gz
        1_131129_BH7VPTADXX_P602_101_2.fastq.gz
    becomes
        { "1_131129_BH7VPTADXX_P602_101":
        [ "1_131129_BH7VPTADXX_P602_101_1.fastq.gz",
          "1_131129_BH7VPTADXX_P602_101_2.fastq.gz"]}

    :param list file_list: A list... of files
    :param str dir: The directory to search for fastq file pairs.
    :returns: A dict of file_basename -> [file1, file2]
    :rtype: dict
    """
    if not directory or file_list:
        raise RuntimeError("Must specify either a list of files or a directory path (in kw format.")
    if file_list and type(file_list) is not list:
        LOG.warn("file_list parameter passed is not a list; trying as a directory.")
        directory = file_list
        file_list = None
    ## TODO What exceptions can be thrown here? Permissions, dir not accessible, ...
    if directory:
        file_list = glob.glob(os.path.join(directory, "*"))
    # We only want fastq files
    if file_list:
        pt = re.compile(".*\.(fastq|fq)(\.gz|\.gzip|\.bz2)?$")
        file_list = filter(pt.match, file_list)
    else:
        # No files found
        return {}
    # --> This is the SciLifeLab-Sthlm-specific format (obsolete as of August 1st, hopefully)
    #     Format: <lane>_<date>_<flowcell>_<project-sample>_<read>.fastq.gz
    #     Example: 1_140220_AH8AMJADXX_P673_101_1.fastq.gz
    # --> This is the standard Illumina/Uppsala format (and Sthlm -> August 1st 2014)
    #     Format: <sample_name>_<index>_<lane>_<read>_<group>.fastq.gz
    #     Example: NA10860_NR_TAAGGC_L005_R1_001.fastq.gz
    suffix_pattern = re.compile(r'(.*)fastq')
    # Cut off at the read group
    file_format_pattern = re.compile(r'(.*)_(?:R\d|\d\.).*')
    matches_dict = collections.defaultdict(list)
    for file_pathname in file_list:
        file_basename = os.path.basename(file_pathname)
        try:
            # See if there's a pair!
            pair_base = file_format_pattern.match(file_basename).groups()[0]
            matches_dict[pair_base].append(os.path.abspath(file_pathname))
        except AttributeError:
            LOG.warn("Warning: file doesn't match expected file format, "
                      "cannot be paired: \"{}\"".format(file_fullname))
            try:
                # File could not be paired, but be the bigger person and include it in the group anyway
                file_basename_stripsuffix = suffix_pattern.split(file_basename)[0]
                matches_dict[file_basename_stripsuffix].append(os.abspath(file_fullname))
            except AttributeError:
                # ??
                continue
    return dict(matches_dict)


@memoized
def get_flowcell_id_from_dirtree(path):
    """Given the path to a file, tries to work out the flowcell ID.

    Project directory structure is generally either:
        <date>_<flowcell>/Sample_<project-sample-id>/
         131018_D00118_0121_BC2NANACXX/Sample_NA10860_NR/
        (Uppsala format)
    or:
        <project>/<project-sample-id>/<date>_<flowcell>/
        G.Spong_13_03/P673_101/140220_AH8AMJADXX/
        (Sthlm format)
    :param str path: The path to the file
    :returns: The flowcell ID
    :rtype: str
    :raises ValueError: If the flowcell ID cannot be determined
    """
    flowcell_pattern = re.compile(r'\d{4,6}_(?P=<fcid>[A-Z0-9]{10})')
    try:
        # SciLifeLab Sthlm tree format (3-dir)
        path, dirname = os.path.split(path)
        return flowcell_pattern.match(dirname).groups()[0]
    except (IndexError, AttributeError):
        try:
            # SciLifeLab Uppsala tree format (2-dir)
            _, dirname = os.path.split(path)
            return flowcell_pattern.match(dirname).groups()[0]
        except (IndexError, AttributeError):
            raise ValueError("Could not determine flowcell ID from directory path.")


## TODO make these hashable (__hash__, __eq__) in some meaningful way
## TODO Add path checking, os.path.abspath / os.path.exists
class NGIObject(object):
    def __init__(self, name, dirname, subitem_type):
        self.name = name
        self.dirname = dirname
        self._subitems = {}
        self._subitem_type = subitem_type

    def _add_subitem(self, name, dirname):
        # Only add a new item if the same item doesn't already exist
        try:
            subitem = self._subitems[name]
        except KeyError:
            subitem = self._subitems[name] = self._subitem_type(name, dirname)
        return subitem

    def __iter__(self):
        return iter(self._subitems.values())

    def __unicode__(self):
        return self.name

    def __str__(self):
        return self.__unicode__()

    def __repr__(self):
        return "{}: \"{}\"".format(type(self), self.name)


class NGIProject(NGIObject):
    def __init__(self, name, dirname, base_path):
        self.base_path = base_path
        super(NGIProject, self).__init__(name, dirname, subitem_type=NGISample)
        self.samples = self._subitems
        self.add_sample = self._add_subitem
        self.command_lines = []


class NGISample(NGIObject):
    def __init__(self, *args, **kwargs):
        super(NGISample, self).__init__(subitem_type=NGIFCID, *args, **kwargs)
        self.fcids = self._subitems
        self.add_fcid = self._add_subitem


class NGIFCID(NGIObject):
    def __init__(self, *args, **kwargs):
        super(NGIFCID, self).__init__(subitem_type=None, *args, **kwargs)
        self.fastqs = self._subitems = []
        ## Not working
        #delattr(self, "_add_subitem")

    def __iter__(self):
        return iter(self._subitems)

    def add_fastq_files(self, fastq):
        if type(fastq) == list:
            self._subitems.extend(fastq)
        elif type(fastq) == str:
            self._subitems.append(fastq)
        else:
            raise TypeError("Fastq files must be passed as a list or a string: " \
                            "got \"{}\"".format(fastq))


if __name__=="__main__":
    parser = argparse.ArgumentParser("Sort and transfer a demultiplxed illumina run.")
    parser.add_argument("--config",
            help="The path to the configuration file.")
    parser.add_argument("--project", action="append",
            help="Restrict processing to these projects. "\
                 "Use flag multiple times for multiple projects.")
    parser.add_argument("--sample", action="append",
            help="Restrict processing to these samples. "\
                 "Use flag multiple times for multiple projects.")
    parser.add_argument("demux_fcid_dir", nargs='*', action="store",
            help="The path to the Illumina demultiplexed fc directories to process. "\
                 "If not specified, new data will be checked for in the "\
                 "\"INBOX\" directory specifiedin the configuration file.")

    args_ns = parser.parse_args()
    main(config_file_path=args_ns.config,
         demux_fcid_dirs=args_ns.demux_fcid_dir,
         restrict_to_projects=args_ns.project,
         restrict_to_samples=args_ns.sample)

