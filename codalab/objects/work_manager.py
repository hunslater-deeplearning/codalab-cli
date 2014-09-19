'''
Worker is a class that executes bundles that need to be run.

It requires direct access to the bundle database and data store, and it
provides a few methods once it is initialized:
  update_created_bundles: update bundles that are blocking on others.
  update_ready_bundles: run a single bundle in the ready state.
'''
import contextlib
import datetime
import random
import subprocess
import sys
import time
import tempfile
import traceback
import os

from codalab.common import (
  precondition,
  State,
  Command,
  UsageError,
)
from codalab.lib import (
  canonicalize,
  path_util,
)
from codalab.bundles.run_bundle import RunBundle
from codalab.bundles.make_bundle import MakeBundle
from codalab.machines import (
  remote_machine,
)

class Worker(object):
    def __init__(self, bundle_store, model, machine):
        self.bundle_store = bundle_store
        self.model = model
        self.profiling_depth = 0
        self.verbose = 0
        self.machine = machine
        self.bundle_data = {}

    def pretty_print(self, message):
        time_str = datetime.datetime.utcnow().isoformat()[:19].replace('T', ' ')
        print '%s: %s%s' % (time_str, '  '*self.profiling_depth, message)

    @contextlib.contextmanager
    def profile(self, message):
        #self.pretty_print(message)
        self.profiling_depth += 1
        start_time = time.time()
        result = yield
        elapsed_time = time.time() - start_time
        self.profiling_depth -= 1
        #if result: self.pretty_print('%s: %0.2fs.' % (message, elapsed_time,))
        #self.pretty_print('Done! Took %0.2fs.' % (elapsed_time,))

    def update_bundle_states(self, bundles, new_state):
        '''
        Update a list of bundles all in one state to all be in the new_state.
        Return True if all updates succeed.
        '''
        if bundles:
            message = 'Setting %s bundles to %s...' % (
              len(bundles),
              new_state.upper(),
            )
            with self.profile(message):
                states = set(bundle.state for bundle in bundles)
                precondition(len(states) == 1, 'Got multiple states: %s' % (states,))
                success = self.model.batch_update_bundles(
                  bundles=bundles,
                  update={'state': new_state},
                  condition={'state': bundles[0].state},
                )
                if not success and self.verbose >= 1:
                    self.pretty_print('WARNING: update failed!')
                return success
        return True

    # Poll processes to see if bundles have finished running
    def check_finished_bundles(self):
        result = self.machine.poll()
        if result:
            self.finalize_bundle(result)

    def start_bundle(self, bundle):
        '''
        Run the given bundle using an available Machine.
        Return 
        '''
        # Check that we're running a bundle in the RUNNING state.
        state_message = 'Unexpected bundle state: %s' % (bundle.state,)
        precondition(bundle.state == State.RUNNING, state_message)
        data_hash_message = 'Unexpected bundle data_hash: %s' % (bundle.data_hash,)
        precondition(bundle.data_hash is None, data_hash_message)

        # Compute a dict mapping parent_uuid -> parent for each dep of this bundle.
        parent_uuids = set(dep.parent_uuid for dep in bundle.dependencies)
        parents = self.model.batch_get_bundles(uuid=parent_uuids)
        parent_dict = {parent.uuid: parent for parent in parents}

        # Store data needed by finalize_bundle method
        self.bundle_data[bundle.uuid] = {
            'parent_dict': parent_dict,
            'start_time': time.time(),
            'actions': [],  # Actions performed on this bundle
        }

        # Run the bundle.
        with self.profile('Running bundle...'):
            started = False
            exception = None
            if isinstance(bundle, RunBundle):
                try:
                    started = self.machine.start_bundle(bundle, self.bundle_store, parent_dict)
                except Exception as e:
                    # TODO: handle this more gracefully
                    # Currently, if there's an exception, we just make the
                    # bundle fail (even if it's not the bundle's fault) so we
                    # don't keep on trying it.
                    started = True
                    exception = e
                    print 'INTERNAL ERROR: %s' % e
            else:
                started = True
            if started: print '-- START BUNDLE: %s' % (bundle,)

            # Run bundle which failed already
            if exception:
                success = False
                temp_dir = canonicalize.get_current_location(self.bundle_store, bundle.uuid)
                path_util.make_directory(temp_dir)
                result = (bundle, success, temp_dir)
                self.finalize_bundle(result)

            # If we have a MakeBundle, then just process it immediately.
            if not isinstance(bundle, RunBundle):
                success = True
                temp_dir = canonicalize.get_current_location(self.bundle_store, bundle.uuid)
                path_util.make_directory(temp_dir)
                result = (bundle, success, temp_dir)
                self.finalize_bundle(result)

            return started

    def check_killed_bundles(self):
        '''
        For bundles that need to be killed, tell the machine to kill it.
        '''
        bundle_actions = self.model.pop_bundle_actions()
        if self.verbose >= 2: print 'bundle_actions:', bundle_actions
        keep_bundle_actions = []
        for x in bundle_actions:
            # TODO: generalize this to other commands
            processed = False
            if x.action == Command.KILL:
                if self.machine.kill_bundle(x.bundle_uuid):
                    processed = True
            if processed:
                self.bundle_data[x.bundle_uuid]['actions'].append(x.action)
            else:
                keep_bundle_actions.append(x)
        if len(keep_bundle_actions) > 0:
            self.model.add_bundle_actions(keep_bundle_actions)
        return len(bundle_actions) - len(keep_bundle_actions) > 0

    def finalize_bundle(self, result):
        (bundle, success, temp_dir) = result

        end_time = time.time()
        bundle_data = self.bundle_data[bundle.uuid]
        start_time = bundle_data['start_time']
        parent_dict = bundle_data['parent_dict']
        actions = bundle_data['actions']

        # Re-install dependencies as relative dependencies
        try:
            bundle.install_dependencies(self.bundle_store, parent_dict, temp_dir, relative_symlinks=True)
            (data_hash, metadata) = self.bundle_store.upload(temp_dir)
        except Exception as e:
            (data_hash, metadata) = (None, {})
            success = False
            metadata['failure_message'] = e.message

        # Update data, remove temp_dir and process
        if isinstance(bundle, RunBundle):
            metadata.update({'time': end_time - start_time})
            if len(actions) > 0:
                metadata.update({'actions': actions})
        state = State.READY if success else State.FAILED

        # Update a bundle to the new state and data hash at the end of a run.
        update = {'state': state, 'data_hash': data_hash}
        if metadata:
            update['metadata'] = metadata
        with self.profile('Setting 1 bundle to %s...' % (state.upper(),)):
            self.model.update_bundle(bundle, update)

        # Remove temporary data
        if isinstance(bundle, RunBundle):
            self.machine.finalize_bundle(bundle.uuid)
        else:
            path_util.remove(temp_dir)

        print '-- END BUNDLE: %s [%s]' % (bundle, state)
        print ''

    def update_created_bundles(self):
        '''
        Scan through CREATED bundles check their dependencies' statuses.
        If any parent is FAILED, move them to FAILED.
        If all parents are READY, move them to STAGED.
        Return whether something happened
        '''
        #print '-- Updating CREATED bundles! --'
        with self.profile('Getting CREATED bundles...'):
            bundles = self.model.batch_get_bundles(state=State.CREATED)
            if self.verbose >= 1 and len(bundles) > 0:
                self.pretty_print('Updating %s created bundles.' % (len(bundles),))
        parent_uuids = set(
          dep.parent_uuid for bundle in bundles for dep in bundle.dependencies
        )

        with self.profile('Getting parents...'):
            parents = self.model.batch_get_bundles(uuid=parent_uuids)
        all_parent_states = {parent.uuid: parent.state for parent in parents}
        all_parent_uuids = set(all_parent_states)
        bundles_to_fail = []
        bundles_to_stage = []
        for bundle in bundles:
            parent_uuids = set(dep.parent_uuid for dep in bundle.dependencies)
            missing_uuids = parent_uuids - all_parent_uuids
            # If uuid doesn't exist, then don't process this bundle yet (the dependency might show up later)
            if missing_uuids: continue
            parent_states = {uuid: all_parent_states[uuid] for uuid in parent_uuids}
            failed_uuids = [
              uuid for (uuid, state) in parent_states.iteritems()
              if state == State.FAILED
            ]
            if failed_uuids:
                bundles_to_fail.append(
                  (bundle, 'Parent bundles failed: %s' % (', '.join(failed_uuids),)))
            elif all(state == State.READY for state in parent_states.itervalues()):
                bundles_to_stage.append(bundle)

        with self.profile('Failing %s bundles...' % (len(bundles_to_fail),)):
            for (bundle, failure_message) in bundles_to_fail:
                metadata_update = {'failure_message': failure_message}
                update = {'state': State.FAILED, 'metadata': metadata_update}
                self.model.update_bundle(bundle, update)
        self.update_bundle_states(bundles_to_stage, State.STAGED)
        num_processed = len(bundles_to_fail) + len(bundles_to_stage)
        num_blocking  = len(bundles) - num_processed
        if num_processed > 0:
            self.pretty_print('%s CREATED bundles => %s STAGED, %s FAILED; %s bundles still waiting on dependencies.' % \
                (num_processed, len(bundles_to_stage), len(bundles_to_fail), num_blocking,))
            return True
        return False

    def update_staged_bundles(self):
        '''
        If there are any STAGED bundles, pick one and try to lock it.
        If we get a lock, move the locked bundle to RUNNING and then run it.
        '''
        #print '-- Updating STAGED bundles! --'
        with self.profile('Getting STAGED bundles...'):
            bundles = self.model.batch_get_bundles(state=State.STAGED)
            if self.verbose >= 1 and len(bundles) > 0:
                self.pretty_print('Staging %s bundles.' % (len(bundles),))
        new_running_bundles = 0
        for bundle in bundles:
            if not self.update_bundle_states([bundle], State.RUNNING):
                self.pretty_print('WARNING: Bundle running, but state failed to update')
            else:
                if self.start_bundle(bundle):
                    new_running_bundles += 1
                else:
                    # Restage: undo state change to RUNNING
                    self.update_bundle_states([bundle], State.STAGED)
        else:
            if self.verbose >= 2: self.pretty_print('Failed to lock a bundle!')
        return new_running_bundles > 0

    def run_loop(self, num_iterations, sleep_time):
        '''
        Repeat forever (if iterations != None) or for a finite number of iterations.
        Moves created bundles to staged and actually executes the staged bundles.
        '''
        self.pretty_print('Running worker loop (num_iterations = %s, sleep_time = %s)' % (num_iterations, sleep_time))
        iteration = 0
        while not num_iterations or iteration < num_iterations:
            # Check to see if any bundles should be killed
            bool_killed = self.check_killed_bundles()
            # Try to stage bundles
            self.update_created_bundles()
            # Try to run bundles with Ready parents
            bool_run = self.update_staged_bundles()
            # Check to see if any bundles are done running
            bool_done = self.check_finished_bundles()

            # Sleep only if nothing happened.
            if not (bool_killed or bool_run or bool_done):
                time.sleep(sleep_time)
            else:
                # Advance counter only if something interesting happened
                iteration += 1