"""Main entry point for distributed next-gen sequencing pipelines.

Handles running the full pipeline based on instructions
"""
import abc
from collections import defaultdict
import copy
import os
import sys
import resource
import tempfile

import toolz as tz
import yaml

from bcbio import log, heterogeneity, hla, structural, utils
from bcbio.distributed import prun
from bcbio.distributed.transaction import tx_tmpdir
from bcbio.log import logger
from bcbio.ngsalign import alignprep
from bcbio.pipeline import datadict as dd
from bcbio.pipeline import (archive, config_utils, disambiguate, region,
                            run_info, qcsummary, rnaseq)
from bcbio.provenance import profile, system
from bcbio.variation import ensemble, genotype, population, validate, joint

def run_main(workdir, config_file=None, fc_dir=None, run_info_yaml=None,
             parallel=None, workflow=None):
    """Run variant analysis, handling command line options.
    """
    workdir = utils.safe_makedir(os.path.abspath(workdir))
    os.chdir(workdir)
    config, config_file = config_utils.load_system_config(config_file, workdir)
    if config.get("log_dir", None) is None:
        config["log_dir"] = os.path.join(workdir, "log")
    if parallel["type"] in ["local", "clusterk"]:
        _setup_resources()
        _run_toplevel(config, config_file, workdir, parallel,
                      fc_dir, run_info_yaml)
    elif parallel["type"] == "ipython":
        assert parallel["scheduler"] is not None, "IPython parallel requires a specified scheduler (-s)"
        if parallel["scheduler"] != "sge":
            assert parallel["queue"] is not None, "IPython parallel requires a specified queue (-q)"
        elif not parallel["queue"]:
            parallel["queue"] = ""
        _run_toplevel(config, config_file, workdir, parallel,
                      fc_dir, run_info_yaml)
    else:
        raise ValueError("Unexpected type of parallel run: %s" % parallel["type"])

def _setup_resources():
    """Attempt to increase resource limits up to hard limits.

    This allows us to avoid out of file handle limits where we can
    move beyond the soft limit up to the hard limit.
    """
    target_procs = 10240
    cur_proc, max_proc = resource.getrlimit(resource.RLIMIT_NPROC)
    target_proc = min(max_proc, target_procs) if max_proc > 0 else target_procs
    resource.setrlimit(resource.RLIMIT_NPROC, (max(cur_proc, target_proc), max_proc))
    cur_hdls, max_hdls = resource.getrlimit(resource.RLIMIT_NOFILE)
    target_hdls = min(max_hdls, target_procs) if max_hdls > 0 else target_procs
    resource.setrlimit(resource.RLIMIT_NOFILE, (max(cur_hdls, target_hdls), max_hdls))

def _run_toplevel(config, config_file, work_dir, parallel,
                  fc_dir=None, run_info_yaml=None):
    """
    Run toplevel analysis, processing a set of input files.
    config_file -- Main YAML configuration file with system parameters
    fc_dir -- Directory of fastq files to process
    run_info_yaml -- YAML configuration file specifying inputs to process
    """
    parallel = log.create_base_logger(config, parallel)
    log.setup_local_logging(config, parallel)
    dirs = run_info.setup_directories(work_dir, fc_dir, config, config_file)
    config_file = os.path.join(dirs["config"], os.path.basename(config_file))
    pipelines, config = _pair_samples_with_pipelines(run_info_yaml, config)
    system.write_info(dirs, parallel, config)
    with tx_tmpdir(config) as tmpdir:
        tempfile.tempdir = tmpdir
        for pipeline, samples in pipelines.items():
            for xs in pipeline.run(config, run_info_yaml, parallel, dirs, samples):
                pass

# ## Generic pipeline framework

def _wres(parallel, progs, fresources=None, ensure_mem=None):
    """Add resource information to the parallel environment on required programs and files.

    Enables spinning up required machines and operating in non-shared filesystem
    environments.

    progs -- Third party tools used in processing
    fresources -- Required file-based resources needed. These will be transferred on non-shared
                  filesystems.
    ensure_mem -- Dictionary of required minimum memory for programs used. Ensures
                  enough memory gets allocated on low-core machines.
    """
    parallel = copy.deepcopy(parallel)
    parallel["progs"] = progs
    if fresources:
        parallel["fresources"] = fresources
    if ensure_mem:
        parallel["ensure_mem"] = ensure_mem
    return parallel

class AbstractPipeline:
    """
    Implement this class to participate in the Pipeline abstraction.
    name: the analysis name in the run_info.yaml file:
        design:
            - analysis: name
    run: the steps run to perform the analyses
    """
    __metaclass__ = abc.ABCMeta

    @abc.abstractproperty
    def name(self):
        return

    @abc.abstractmethod
    def run(self, config, run_info_yaml, parallel, dirs, samples):
        return

class _WorldWatcher:
    """Watch changes in the world and output directory and report.

    Used to create input files we can feed into CWL creation about
    the changed state of the world.
    """
    def __init__(self, work_dir, is_on=True):
        self._work_dir = work_dir
        self._is_on = is_on
        if not self._is_on:
            return
        self._out_dir = utils.safe_makedir(os.path.join(work_dir, "world2cwl"))
        self._lworld = {}
        self._lfiles = set([])

    def _find_files(self):
        out = []
        for (dir, _, files) in os.walk(self._work_dir):
            out += [os.path.join(dir, f).replace(self._work_dir + "/", "") for f in files]
        return set(out)

    def _items_to_world(self, items):
        world = {}
        for item in items:
            assert len(item) == 1
            world[dd.get_sample_name(item[0])] = item[0]
        return world

    def _compare_dicts(self, orig, new, ns):
        out = {}
        for key, val in new.items():
            nskey = ns + [key]
            orig_val = tz.get_in([key], orig)
            if isinstance(val, dict) and isinstance(orig_val, dict):
                for nkey, nval in self._compare_dicts(orig_val or {}, val or {}, nskey).items():
                    out = tz.update_in(out, [nkey], lambda x: nval)
            elif val != orig_val:
                print nskey, val, orig_val
                out = tz.update_in(out, nskey, lambda x: val)
        return out

    def initialize(self, world):
        if not self._is_on:
            return
        self._lfiles = self._find_files()
        self._lworld = self._items_to_world(world)

    def report(self, step, world):
        if not self._is_on:
            return
        new_files = self._find_files()
        file_changes = new_files - self._lfiles
        self._lfiles = new_files
        world_changes = self._compare_dicts(self._lworld, self._items_to_world(world), [])
        self._lworld = world
        import pprint
        print step
        pprint.pprint(file_changes)
        pprint.pprint(world_changes)

class Variant2Pipeline(AbstractPipeline):
    """Streamlined variant calling pipeline for large files.
    This is less generalized but faster in standard cases.
    The goal is to replace the base variant calling approach.
    """
    name = "variant2"

    @classmethod
    def run(self, config, run_info_yaml, parallel, dirs, samples):
        ## Alignment and preparation requiring the entire input file (multicore cluster)
        with prun.start(_wres(parallel, ["aligner", "samtools", "sambamba"],
                              (["reference", "fasta"], ["reference", "aligner"], ["files"])),
                        samples, config, dirs, "multicore",
                        multiplier=alignprep.parallel_multiplier(samples)) as run_parallel:
            with profile.report("organize samples", dirs):
                samples = run_parallel("organize_samples", [[dirs, config, run_info_yaml,
                                                             [x[0]["description"] for x in samples]]])
            ww = _WorldWatcher(dirs["work"], is_on=any([dd.get_cwl_reporting(d[0]) for d in samples]))
            ww.initialize(samples)
            with profile.report("alignment preparation", dirs):
                samples = run_parallel("prep_align_inputs", samples)
                ww.report("prep_align_inputs", samples)
                samples = run_parallel("disambiguate_split", [samples])
            with profile.report("alignment", dirs):
                samples = run_parallel("process_alignment", samples)
                samples = disambiguate.resolve(samples, run_parallel)
                samples = alignprep.merge_split_alignments(samples, run_parallel)
            with profile.report("callable regions", dirs):
                samples = run_parallel("prep_samples", [samples])
                samples = run_parallel("postprocess_alignment", samples)
                samples = run_parallel("combine_sample_regions", [samples])
                samples = region.clean_sample_data(samples)
            with profile.report("structural variation initial", dirs):
                samples = structural.run(samples, run_parallel, "initial")
            with profile.report("hla typing", dirs):
                samples = hla.run(samples, run_parallel)

        ## Variant calling on sub-regions of the input file (full cluster)
        with prun.start(_wres(parallel, ["gatk", "picard", "variantcaller"]),
                        samples, config, dirs, "full",
                        multiplier=region.get_max_counts(samples), max_multicore=1) as run_parallel:
            with profile.report("alignment post-processing", dirs):
                samples = region.parallel_prep_region(samples, run_parallel)
            with profile.report("variant calling", dirs):
                samples = genotype.parallel_variantcall_region(samples, run_parallel)

        ## Finalize variants, BAMs and population databases (per-sample multicore cluster)
        with prun.start(_wres(parallel, ["gatk", "gatk-vqsr", "snpeff", "bcbio_variation",
                                         "gemini", "samtools", "fastqc", "bamtools",
                                         "bcbio-variation-recall", "qsignature",
                                         "svcaller"]),
                        samples, config, dirs, "multicore2",
                        multiplier=structural.parallel_multiplier(samples)) as run_parallel:
            with profile.report("joint squaring off/backfilling", dirs):
                samples = joint.square_off(samples, run_parallel)
            with profile.report("variant post-processing", dirs):
                samples = run_parallel("postprocess_variants", samples)
                samples = run_parallel("split_variants_by_sample", samples)
            with profile.report("prepped BAM merging", dirs):
                samples = region.delayed_bamprep_merge(samples, run_parallel)
            with profile.report("validation", dirs):
                samples = run_parallel("compare_to_rm", samples)
                samples = genotype.combine_multiple_callers(samples)
            with profile.report("ensemble calling", dirs):
                samples = ensemble.combine_calls_parallel(samples, run_parallel)
            with profile.report("validation summary", dirs):
                samples = validate.summarize_grading(samples)
            with profile.report("structural variation final", dirs):
                samples = structural.run(samples, run_parallel, "standard")
            with profile.report("structural variation ensemble", dirs):
                samples = structural.run(samples, run_parallel, "ensemble")
            with profile.report("structural variation validation", dirs):
                samples = run_parallel("validate_sv", samples)
            with profile.report("heterogeneity", dirs):
                samples = heterogeneity.run(samples, run_parallel)
            with profile.report("population database", dirs):
                samples = population.prep_db_parallel(samples, run_parallel)
            with profile.report("quality control", dirs):
                samples = qcsummary.generate_parallel(samples, run_parallel)
            with profile.report("archive", dirs):
                samples = archive.compress(samples, run_parallel)
            with profile.report("upload", dirs):
                samples = run_parallel("upload_samples", samples)
                for sample in samples:
                    run_parallel("upload_samples_project", [sample])
        logger.info("Timing: finished")
        return samples

def _debug_samples(i, samples):
    print "---", i, len(samples)
    for sample in (x[0] for x in samples):
        print "  ", sample["description"], sample.get("region"), \
            utils.get_in(sample, ("config", "algorithm", "variantcaller")), \
            utils.get_in(sample, ("config", "algorithm", "jointcaller")), \
            utils.get_in(sample, ("metadata", "batch")), \
            [x.get("variantcaller") for x in sample.get("variants", [])], \
            sample.get("work_bam"), \
            sample.get("vrn_file")

class SNPCallingPipeline(Variant2Pipeline):
    """Back compatible: old name for variant analysis.
    """
    name = "SNP calling"

class VariantPipeline(Variant2Pipeline):
    """Back compatibility; old name
    """
    name = "variant"

class StandardPipeline(AbstractPipeline):
    """Minimal pipeline with alignment and QC.
    """
    name = "Standard"
    @classmethod
    def run(self, config, run_info_yaml, parallel, dirs, samples):
        ## Alignment and preparation requiring the entire input file (multicore cluster)
        with prun.start(_wres(parallel, ["aligner", "samtools", "sambamba"]),
                        samples, config, dirs, "multicore") as run_parallel:
            with profile.report("organize samples", dirs):
                samples = run_parallel("organize_samples", [[dirs, config, run_info_yaml,
                                                             [x[0]["description"] for x in samples]]])
            with profile.report("alignment", dirs):
                samples = run_parallel("process_alignment", samples)
            with profile.report("callable regions", dirs):
                samples = run_parallel("prep_samples", [samples])
                samples = run_parallel("postprocess_alignment", samples)
                samples = run_parallel("combine_sample_regions", [samples])
                samples = region.clean_sample_data(samples)
        ## Quality control
        with prun.start(_wres(parallel, ["fastqc", "bamtools", "qsignature", "kraken", "gatk", "samtools"]),
                        samples, config, dirs, "multicore2") as run_parallel:
            with profile.report("quality control", dirs):
                samples = qcsummary.generate_parallel(samples, run_parallel)
            with profile.report("upload", dirs):
                samples = run_parallel("upload_samples", samples)
                for sample in samples:
                    run_parallel("upload_samples_project", [sample])
        logger.info("Timing: finished")
        return samples

class MinimalPipeline(StandardPipeline):
    name = "Minimal"

class SailfishPipeline(AbstractPipeline):
    name = "sailfish"

    @classmethod
    def run(self, config, run_info_yaml, parallel, dirs, samples):
        with prun.start(_wres(parallel, ["picard", "cutadapt"]),
                        samples, config, dirs, "trimming") as run_parallel:
            with profile.report("organize samples", dirs):
                samples = run_parallel("organize_samples", [[dirs, config, run_info_yaml,
                                                             [x[0]["description"] for x in samples]]])
            with profile.report("adapter trimming", dirs):
                samples = run_parallel("prepare_sample", samples)
                samples = run_parallel("trim_sample", samples)
            with prun.start(_wres(parallel, ["sailfish"]), samples, config, dirs,
                            "sailfish") as run_parallel:
                with profile.report("sailfish", dirs):
                    samples = run_parallel("run_sailfish", samples)
                with profile.report("upload", dirs):
                    samples = run_parallel("upload_samples", samples)
                    for sample in samples:
                        run_parallel("upload_samples_project", [sample])
        return samples

class RnaseqPipeline(AbstractPipeline):
    name = "RNA-seq"

    @classmethod
    def run(self, config, run_info_yaml, parallel, dirs, samples):
        with prun.start(_wres(parallel, ["picard", "cutadapt"]),
                        samples, config, dirs, "trimming", max_multicore=1) as run_parallel:
            with profile.report("organize samples", dirs):
                samples = run_parallel("organize_samples", [[dirs, config, run_info_yaml,
                                                             [x[0]["description"] for x in samples]]])
            with profile.report("adapter trimming", dirs):
                samples = run_parallel("prepare_sample", samples)
                samples = run_parallel("trim_sample", samples)
        with prun.start(_wres(parallel, ["aligner", "picard"],
                              ensure_mem={"tophat": 10, "tophat2": 10, "star": 2, "hisat2": 8}),
                        samples, config, dirs, "alignment",
                        multiplier=alignprep.parallel_multiplier(samples)) as run_parallel:
            with profile.report("alignment", dirs):
                samples = run_parallel("disambiguate_split", [samples])
                samples = run_parallel("process_alignment", samples)
        with prun.start(_wres(parallel, ["samtools", "cufflinks", "sailfish"]),
                        samples, config, dirs, "rnaseqcount") as run_parallel:
            with profile.report("disambiguation", dirs):
                samples = disambiguate.resolve(samples, run_parallel)
            with profile.report("transcript assembly", dirs):
                samples = rnaseq.assemble_transcripts(run_parallel, samples)
            with profile.report("estimate expression (threaded)", dirs):
                samples = rnaseq.quantitate_expression_parallel(samples, run_parallel)
        with prun.start(_wres(parallel, ["dexseq", "express"]), samples, config,
                        dirs, "rnaseqcount-singlethread", max_multicore=1) as run_parallel:
            with profile.report("estimate expression (single threaded)", dirs):
                samples = rnaseq.quantitate_expression_noparallel(samples, run_parallel)
        samples = rnaseq.combine_files(samples)
        with prun.start(_wres(parallel, ["gatk"]), samples, config,
                        dirs, "rnaseq-variation") as run_parallel:
            with profile.report("RNA-seq variant calling", dirs):
                samples = rnaseq.rnaseq_variant_calling(samples, run_parallel)

        with prun.start(_wres(parallel, ["samtools", "fastqc", "qualimap",
                                         "kraken", "gatk"], ensure_mem={"qualimap": 4}),
                        samples, config, dirs, "qc") as run_parallel:
            with profile.report("quality control", dirs):
                samples = qcsummary.generate_parallel(samples, run_parallel)
            with profile.report("upload", dirs):
                samples = run_parallel("upload_samples", samples)
                for sample in samples:
                    run_parallel("upload_samples_project", [sample])
        logger.info("Timing: finished")
        return samples

class smallRnaseqPipeline(AbstractPipeline):
    name = "smallRNA-seq"

    @classmethod
    def run(self, config, run_info_yaml, parallel, dirs, samples):
        # causes a circular import at the top level
        from bcbio.srna.group import report as srna_report

        with prun.start(_wres(parallel, ["picard", "cutadapt"]),
                        samples, config, dirs, "trimming") as run_parallel:
            with profile.report("organize samples", dirs):
                samples = run_parallel("organize_samples", [[dirs, config, run_info_yaml,
                                                             [x[0]["description"] for x in samples]]])
            with profile.report("adapter trimming", dirs):
                samples = run_parallel("prepare_sample", samples)
                samples = run_parallel("trim_srna_sample", samples)

        with prun.start(_wres(parallel, ["aligner", "picard", "samtools"],
                              ensure_mem={"bowtie": 8, "bowtie2": 8, "star": 2}),
                        [samples[0]], config, dirs, "alignment") as run_parallel:
            with profile.report("prepare", dirs):
                samples = run_parallel("seqcluster_prepare", [samples])
            with profile.report("alignment", dirs):
                samples = run_parallel("srna_alignment", [samples])

        with prun.start(_wres(parallel, ["picard", "miraligner"]),
                        samples, config, dirs, "annotation") as run_parallel:
            with profile.report("small RNA annotation", dirs):
                samples = run_parallel("srna_annotation", samples)

        with prun.start(_wres(parallel, ["seqcluster"],
                              ensure_mem={"seqcluster": 8}),
                        [samples[0]], config, dirs, "cluster") as run_parallel:
            with profile.report("cluster", dirs):
                samples = run_parallel("seqcluster_cluster", [samples])

        with prun.start(_wres(parallel, ["picard", "fastqc"]),
                        samples, config, dirs, "qc") as run_parallel:
            with profile.report("quality control", dirs):
                samples = qcsummary.generate_parallel(samples, run_parallel)
            with profile.report("report", dirs):
                srna_report(samples)
            with profile.report("upload", dirs):
                samples = run_parallel("upload_samples", samples)
                for sample in samples:
                    run_parallel("upload_samples_project", [sample])

        return samples

class ChipseqPipeline(AbstractPipeline):
    name = "chip-seq"

    @classmethod
    def run(self, config, run_info_yaml, parallel, dirs, samples):
        with prun.start(_wres(parallel, ["aligner", "picard"]),
                        samples, config, dirs, "multicore",
                        multiplier=alignprep.parallel_multiplier(samples)) as run_parallel:
            with profile.report("organize samples", dirs):
                samples = run_parallel("organize_samples", [[dirs, config, run_info_yaml,
                                                             [x[0]["description"] for x in samples]]])
            samples = run_parallel("prepare_sample", samples)
            samples = run_parallel("trim_sample", samples)
            samples = run_parallel("disambiguate_split", [samples])
            samples = run_parallel("process_alignment", samples)
        with prun.start(_wres(parallel, ["picard", "fastqc"]),
                        samples, config, dirs, "persample") as run_parallel:
            with profile.report("disambiguation", dirs):
                samples = disambiguate.resolve(samples, run_parallel)
            samples = run_parallel("clean_chipseq_alignment", samples)
            samples = qcsummary.generate_parallel(samples, run_parallel)
            with profile.report("upload", dirs):
                samples = run_parallel("upload_samples", samples)
                for sample in samples:
                    run_parallel("upload_samples_project", [sample])
        return samples

def _get_pipeline(item):
    from bcbio.log import logger
    SUPPORTED_PIPELINES = {x.name.lower(): x for x in
                           utils.itersubclasses(AbstractPipeline)}
    analysis_type = item.get("analysis", "").lower()
    if analysis_type not in SUPPORTED_PIPELINES:
        logger.error("Cannot determine which type of analysis to run, "
                      "set in the run_info under details.")
        sys.exit(1)
    else:
        return SUPPORTED_PIPELINES[analysis_type]

def _pair_samples_with_pipelines(run_info_yaml, config):
    """Map samples defined in input file to pipelines to run.
    """
    with open(run_info_yaml) as in_handle:
        samples = yaml.safe_load(in_handle)
        if isinstance(samples, dict):
            resources = samples.pop("resources", {})
            samples = samples["details"]
        else:
            resources = {}
    ready_samples = []
    for sample in samples:
        if "files" in sample:
            del sample["files"]
        # add any resources to this item to recalculate global configuration
        usample = copy.deepcopy(sample)
        usample.pop("algorithm", None)
        if "resources" not in usample:
            usample["resources"] = {}
        for prog, pkvs in resources.iteritems():
            if prog not in usample["resources"]:
                usample["resources"][prog] = {}
            for key, val in pkvs.iteritems():
                usample["resources"][prog][key] = val
        config = config_utils.update_w_custom(config, usample)
        sample["resources"] = {}
        ready_samples.append(sample)
    paired = [(x, _get_pipeline(x)) for x in ready_samples]
    d = defaultdict(list)
    for x in paired:
        d[x[1]].append([x[0]])
    return d, config
