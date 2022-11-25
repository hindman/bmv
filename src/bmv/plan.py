import re
import sys

from copy import deepcopy
from dataclasses import asdict, replace as clone
from itertools import groupby
from os.path import commonprefix
from pathlib import Path

from .constants import (
    CON,
    FAIL,
    CONTROLS,
    CONTROLLABLES,
    STRUCTURES,
    validated_failure_controls,
)
from .data_objects import (
    RenamePair,
    Failure,
    ParseFailure,
    UserCodeExecFailure,
    NoPathsFailure,
    RpFilterFailure,
    RpRenameFailure,
    RpEqualFailure,
    RpMissingFailure,
    RpMissingParentFailure,
    RpExistsFailure,
    RpCollsionFailure,
    BmvError,
)

class RenamingPlan:

    def __init__(self,
                 # Path inputs and their structure.
                 inputs,
                 structure = None,
                 # User code for renaming and filtering.
                 rename_code = None,
                 filter_code = None,
                 indent = 4,
                 # Sequence numbering.
                 seq_start = 1,
                 seq_step = 1,
                 # File system via dependency injection.
                 file_sys = None,
                 # Failure controls.
                 skip_equal = False,
                 skip_missing = False,
                 skip_missing_parent = False,
                 create_missing_parent = False,
                 skip_existing_new = False,
                 clobber_existing_new = False,
                 skip_colliding_new = False,
                 clobber_colliding_new = False,
                 skip_failed_rename = False,
                 skip_failed_filter = False,
                 keep_failed_filter = False,
                 ):

        # Basic attributes passed as arguments into the constructor.
        self.inputs = tuple(inputs)
        self.structure = structure
        self.rename_code = rename_code
        self.filter_code = filter_code
        self.indent = indent
        self.seq_start = seq_start
        self.seq_step = seq_step
        self.file_sys = self.initialize_file_sys(file_sys)

        self.skip_failed_filter = skip_failed_filter
        self.skip_failed_rename = skip_failed_rename
        self.skip_equal = skip_equal
        self.skip_missing = skip_missing
        self.skip_missing_parent = skip_missing_parent
        self.skip_existing_new = skip_existing_new
        self.skip_colliding_new = skip_colliding_new
        self.clobber_existing_new = clobber_existing_new
        self.clobber_colliding_new = clobber_colliding_new
        self.keep_failed_filter = keep_failed_filter
        self.create_missing_parent = create_missing_parent

        # Get a dict mapping each Failure type to the user's requested control
        # mechanism (skip, keep, create, clobber).
        result = validated_failure_controls(self)
        if isinstance(result, Failure):
            raise ValueError(result.msg)
        else:
            self.fail_config = result

        # Failures that occur during the prepare() phase are stored in a dict.
        # A failure can be either controlled (as requested by the user) or not.
        # The dict maps each control mechanism to the failures that were
        # controlled by that mechanism. If the dict ends up having any
        # uncontrollec failures (under the None key), the RenamingPlan will
        # have failed.
        self.failures = {
            control : []
            for control, _ in CONTROLLABLES.values()
        }
        self.failures[None] = []

        # The paths to be renamed will be stored as RenamePair instances.
        self.rps = tuple()

        # Lenth of the longest common prefix string on the original paths.
        self.prefix_len = 0

        self.has_prepared = False
        self.has_renamed = False

    ####
    #
    # Preparation before renaming.
    #
    # This method perform various validations and computations needed before
    # renaming can occur. The method does not raise; rather, when failures
    # occur, they are stored in self.failures based on their type and whether
    # the user has configured the RenamingPlan to handle failures of that kind.
    #
    ####

    def prepare(self):
        # Don't prepare more than once.
        if self.has_prepared:
            return
        else:
            self.has_prepared = True

        # Get the input paths and parse them to get RenamePair instances.
        self.rps, _ = self.catch_failure(self.parse_inputs())
        if self.failed:
            return

        # Create filtering function from user code.
        if self.filter_code:
            self.filter_func, _ = self.catch_failure(self.make_user_defined_func('filter'))
            if self.failed:
                return

        # Create renaming function from user code.
        if self.rename_code:
            self.rename_func, _ = self.catch_failure(self.make_user_defined_func('rename'))
            if self.failed:
                return

        # Run various steps that process the RenamePair instances individually:
        # filtering, computing new paths, or validating.
        #
        # We use the processed_rps() method to execute the step, handle
        # failures appropriately, and yield a potentially filtered collection
        # of potentially modified RenamePair instances.
        #
        # Since filtering can occur at each step (depending on failure-control
        # settings) we check for NoPathsFailure after each step.
        #
        rp_steps = (
            self.execute_user_filter,
            self.execute_user_rename,
            self.check_orig_exists,
            self.check_orig_new_differ,
            self.check_new_not_exists,
            self.check_new_parent_exists,
        )
        for step in rp_steps:
            self.rps = tuple(self.processed_rps(step))
            if not self.rps:
                f = NoPathsFailure(FAIL.no_paths_after_processing)
                self.catch_failure(f)
            if self.failed:
                return

        self.check_new_collisions()
        if self.failed:
            return

    ####
    # Parsing inputs to obtain the original and, in some cases, new paths.
    ####

    def parse_inputs(self):
        # If we have rename_code, inputs are just original paths.
        if self.rename_code:
            rps = tuple(
                RenamePair(orig, None)
                for orig in self.inputs
                if orig
            )
            return rps if rps else ParseFailure(FAIL.no_input_paths)

        # Otherwise, organize inputs into original paths and new paths.
        if self.structure == STRUCTURES.paragraphs:
            # Paragraphs: first original paths, then new paths.
            groups = [
                list(lines)
                for g, lines in groupby(self.inputs, key = bool)
                if g
            ]
            if len(groups) == 2:
                origs, news = groups
            else:
                return ParseFailure(FAIL.parsing_paragraphs)
        elif self.structure == STRUCTURES.flat:
            # Flat: like paragraphs without the blank-line delimiter.
            paths = [line for line in self.inputs if line]
            i = len(paths) // 2
            origs, news = (paths[0:i], paths[i:])
        elif self.structure == STRUCTURES.pairs:
            # Pairs: original path, new path, original path, etc.
            origs = []
            news = []
            current = origs
            for line in self.inputs:
                if line:
                    current.append(line)
                    current = news if current is origs else origs
        elif self.structure == STRUCTURES.rows:
            # Rows: original-new path pairs, as tab-delimited rows.
            origs = []
            news = []
            for row in self.inputs:
                if row:
                    cells = row.split(CON.tab)
                    if len(cells) == 2:
                        origs.append(cells[0])
                        news.append(cells[1])
                    else:
                        return ParseFailure(FAIL.parsing_row.format(row = row))
        else:
            return ParseFailure(FAIL.parsing_no_structures)

        # Fail if we got no paths or unqual numbers of original vs new paths.
        if not origs:
            return ParseFailure(FAIL.no_input_paths)
        elif len(origs) != len(news):
            return ParseFailure(FAIL.parsing_inequality)

        # Return the RenamePair instances.
        return tuple(
            RenamePair(orig, new)
            for orig, new in zip(origs, news)
        )

    ####
    # Creating the user-defined functions for filtering and renaming.
    ####

    def make_user_defined_func(self, action):
        # Define the text of the code.
        func_name = f'do_{action}'
        code = CON.user_code_fmt.format(
            func_name = func_name,
            user_code = getattr(self, f'{action}_code'),
            indent = ' ' * self.indent,
        )
        # Create the function via exec() in the context of:
        # - Globals that we want to make available to the user's code.
        # - A locals dict that we can use to return the generated function.
        globs = dict(
            re = re,
            Path = Path,
        )
        locs = {}
        try:
            exec(code, globs, locs)
            return locs[func_name]
        except Exception as e:
            msg = f''
            return UserCodeExecFailure(msg)

    ####
    # A method to execute the steps that process RenamePair instance individually.
    ####

    def processed_rps(self, step):
        self.prefix_len = self.compute_prefix_len()
        seq = self.compute_sequence_iterator()
        for rp in self.rps:
            # The step() call can return one of three types of values:
            #
            #  - A RenamePairFailure instance if there was a failure.
            #  - A RenamePair instance, possibly modified by the step call.
            #  - None, if the RenamePair should be filtered out.
            #
            result = step(rp, next(seq))

            # And catch_failure() will return that same result inside
            # of a (RESULT, CONTROL) tuple.
            #
            # If the result was not a failure, the control will be None.
            #
            # If the result was a failure, the control will either be a
            # failure-contol value (skip, keep, create, clobber) or None (if
            # the user did not request controls for that type of failure).
            #
            result, control = self.catch_failure(result)
            if control == CONTROLS.skip:
                # Skip RenamePair because a failure occured, but proceed others.
                pass
            elif control == CONTROLS.keep:
                # Retain the RenamePair even though a failure occured during filtering.
                yield rp
            elif control == CONTROLS.create:
                # The RenamePair lacks a parent, but we will create it before renaming.
                yield clone(rp, create_parent = True)
            elif control == CONTROLS.clobber:
                # During renaming, the RenamePair will overwrite something, but that's acceptable.
                yield clone(rp, clobber = True)
            elif result is None:
                # The filtering step succeeded and we want to reject the RenamePair.
                pass
            else:
                # No failure occured and the RenamePair was not filtered out.
                yield result

    ####
    # The steps that process RenamePair instance individually.
    ####

    def execute_user_filter(self, rp, seq_val):
        if self.filter_code:
            try:
                result = self.filter_func(rp.orig, Path(rp.orig), seq_val, self)
                return rp if result else None
            except Exception as e:
                msg = f'Error in user-supplied filtering code: {e} [original path: {rp.orig}]'
                return RpFilterFailure(msg, rp)
        else:
            return rp

    def execute_user_rename(self, rp, seq_val):
        if self.rename_code:
            # Compute the new path.
            try:
                new = self.rename_func(rp.orig, Path(rp.orig), seq_val, self)
            except Exception as e:
                msg = f'Error in user-supplied renaming code: {e} [original path: {rp.orig}]'
                return RpRenameFailure(msg, rp)
            # Validate its type and return a modified RenamePair instance.
            if isinstance(new, (str, Path)):
                return clone(rp, new = str(new))
            else:
                typ = type(new).__name__
                msg = f'Invalid type from user-supplied renaming code: {typ} [original path: {rp.orig}]'
                return RpRenameFailure(msg, rp)
        else:
            return rp

    def check_orig_exists(self, rp, seq_val):
        if self.path_exists(rp.orig):
            return rp
        else:
            return RpMissingFailure(FAIL.orig_missing, rp)

    def check_orig_new_differ(self, rp, seq_val):
        if rp.equal:
            return RpEqualFailure(FAIL.orig_new_same, rp)
        else:
            return rp

    def check_new_not_exists(self, rp, seq_val):
        # The failure is conditional on ORIG and NEW being different
        # to avoid pointless reporting of multiple failures in such cases.
        if self.path_exists(rp.new) and not rp.equal:
            return RpExistsFailure(FAIL.new_exists, rp)
        else:
            return rp

    def check_new_parent_exists(self, rp, seq_val):
        if self.path_exists(str(Path(rp.new).parent)):
            return rp
        else:
            return RpMissingParentFailure(FAIL.new_parent_missing, rp)

    def check_new_collisions(self):
        # Organize rps into dict-of-list, keyed by the new path.
        groups = {}

        # print()
        # print(self.file_sys)
        # for rp in self.rps:
        #     print(rp)
        # print()

        for rp in self.rps:
            groups.setdefault(rp.new, []).append(rp)
        # If any group contains multiple members, add them all as potential failures.
        for g in groups.values():
            if len(g) > 1:
                for rp in g:
                    f = RpCollsionFailure(FAIL.new_collision, rp)
                    self.catch_failure(f)

    ####
    # Methods related to failure control.
    ####

    def catch_failure(self, x):
        # Used when calling other methods to:
        #   - catch a Failure instance,
        #   - store it in self.failures under the appropriate failure-control key,
        #   - forward the value along with the control in a tuple.
        #
        # There are three general types of return values:
        #   - (RenamePairFailure-instance, control-key)   # Failure, controlled.
        #   - (RenamePairFailure-instance, None)          # Failure, uncontrolled.
        #   - (RenamePair-instance, None)                 # Not a failure.
        #
        if isinstance(x, Failure):
            control = self.fail_config.get(type(x), None)
            self.failures[control].append(x)
            return (x, control)
        else:
            return (x, None)

    @property
    def failed(self):
        # The RenamingPlan has failed if there are any uncontrolled failures.
        return bool(self.uncontrolled_failures)

    @property
    def uncontrolled_failures(self):
        return self.failures[None]

    ####
    # Sequence number and common prefix.
    ####

    def compute_sequence_iterator(self):
        return iter(range(self.seq_start, sys.maxsize, self.seq_step))

    def compute_prefix_len(self):
        origs = tuple(rp.orig for rp in self.rps)
        return len(commonprefix(origs))

    def strip_prefix(self, orig):
        i = self.prefix_len
        return orig[i:] if i else orig

    ####
    # Files system operations.
    ####

    def initialize_file_sys(self, file_sys):
        # Currently the file system is stored as a dict mapping each
        # existing path to True. Later, we might need the dict values
        # to hold additional information.
        #
        # We build an independent copy of the file system because
        # the rename_paths() method will modify the dict.
        if file_sys is None:
            return None
        elif isinstance(file_sys, dict):
            return deepcopy(file_sys)
        else:
            return {path : True for path in file_sys}

    def path_exists(self, p):
        if self.file_sys is None:
            # Check the real file system.
            return Path(p).exists()
        else:
            # Or check the fake file system added for testing purposes.
            # In this context, assume that '.' always exists so that the
            # user/tester does not have to include explicitly.
            p = str(p)
            return p in self.file_sys or p == '.'

    def rename_paths(self):
        # Don't rename more than once.
        if self.has_renamed:
            raise BmvError(FAIL.rename_done_already)
        else:
            self.has_renamed = True

        # Ensure than we have prepare, and raise it failed.
        self.prepare()
        if self.failed:
            raise BmvError(FAIL.prepare_failed, failures = self.failures[None])

        # Rename paths.
        if self.file_sys is None:
            # On the real file system.
            for rp in self.rps:
                Path(rp.orig).rename(rp.new)
        else:
            # Or in the fake file system.
            for rp in self.rps:
                self.file_sys[rp.new] = self.file_sys.pop(rp.orig)

    ####
    # The RenamingPlan as a dict.
    ####

    @property
    def as_dict(self):
        return dict(
            inputs = self.inputs,
            structure = self.structure,
            rename_code = self.rename_code,
            filter_code = self.filter_code,
            indent = self.indent,
            seq_start = self.seq_start,
            seq_step = self.seq_step,
            file_sys = self.file_sys,
            skip_failed_filter = self.skip_failed_filter,
            skip_failed_rename = self.skip_failed_rename,
            skip_equal = self.skip_equal,
            skip_missing = self.skip_missing,
            skip_missing_parent = self.skip_missing_parent,
            skip_existing_new = self.skip_existing_new,
            skip_colliding_new = self.skip_colliding_new,
            clobber_existing_new = self.clobber_existing_new,
            clobber_colliding_new = self.clobber_colliding_new,
            keep_failed_filter = self.keep_failed_filter,
            create_missing_parent = self.create_missing_parent,
            failures = self.failures,
            prefix_len = self.prefix_len,
            rename_pairs = [asdict(rp) for rp in self.rps],
        )

