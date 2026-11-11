"""Exercise the real stdio adapter and Store with a deterministic native peer."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from bot_omp_delegate.codex_worker import CodexWorker
from bot_omp_delegate.lifecycle import Conflict, Store, TERMINAL


PEER = r'''
import json, pathlib, sys
root = pathlib.Path(sys.argv[1])
state_file = root / 'native.json'
state = json.loads(state_file.read_text()) if state_file.exists() else {'next': 0, 'threads': {}}
thread = active = pending = start_request = previous = compact = None

def save(): state_file.write_text(json.dumps(state))
def send(frame): print(json.dumps(frame), flush=True)
def reply(request, result=None, error=None):
    send({'id': request['id'], **({'error': error} if error else {'result': result or {}})})
def note(method, **params): send({'method': method, 'params': {'threadId': thread, **params}})
def finish(text='finished', status='completed', error=None):
    global active, previous
    note('item/completed', turnId=active, item={'type':'agentMessage','id':active+'-answer','text':text})
    note('turn/completed', turn={'id':active,'status':status,'error':error,'items':[]})
    previous, active = active, None

for line in sys.stdin:
    frame = json.loads(line)
    method, params = frame.get('method'), frame.get('params', {})
    if method == 'initialize': reply(frame)
    elif method == 'initialized': pass
    elif method == 'thread/start':
        state['next'] += 1
        thread = 'thread-' + str(state['next'])
        save()
        reply(frame, {'thread':{'id':thread}})
    elif method == 'thread/resume':
        if params['threadId'] not in state['threads']:
            reply(frame, error={'code':-32600,'message':'no rollout found'})
        else:
            thread = params['threadId']
            reply(frame, {'thread':{'id':thread}})
    elif method == 'thread/name/set': reply(frame)
    elif method == 'turn/start':
        text = params['input'][0]['text']
        history = state['threads'].setdefault(thread, [])
        history.append(text)
        save()
        active = 'turn-' + str(len(history))
        if text == 'reject':
            reply(frame, error={'code':-32600,'message':'prompt rejected'})
            active = None
            continue
        note('turn/started', turn={'id':active,'status':'inProgress','items':[]})
        if text == 'question':
            pending = 77
            send({'id':pending,'method':'item/tool/requestUserInput','params':{
                'threadId':thread,'turnId':active,'itemId':'ask','isBlocking':True,
                'questions':[{'id':'choice','header':'Choose','question':'Which target?',
                    'options':[{'label':'A','description':'first'},{'label':'B','description':'second'}]}]}})
        if text == 'blocked-start':
            start_request = frame
            continue
        if text == 'successor' and previous:
            note('item/completed', turnId=previous, item={'type':'agentMessage','id':'old','text':'stale'})
            note('turn/completed', turn={'id':previous,'status':'completed','items':[]})
            send({'method':'turn/completed','params':{'threadId':'unrelated','turn':{'id':active,'status':'completed','items':[]}}})
        reply(frame, {'turn':{'id':active,'status':'inProgress','items':[]}})
        if text in ('hold','successor'):
            note('item/completed', turnId=active, item={'type':'agentMessage','id':active+'-partial','text':'partial'})
        elif text in ('approval','file-approval'):
            pending = 88 if text == 'approval' else 'file-88'
            operation = 'commandExecution' if text == 'approval' else 'fileChange'
            send({'id':pending,'method':'item/'+operation+'/requestApproval','params':{
                'threadId':thread,'turnId':active,'itemId':'exec','command':'harmless','reason':'Allow?'}})
        elif text == 'failure':
            note('error', turnId=active, error={'message':'retrying'}, willRetry=True)
        elif text == 'question': pass
        elif text == 'recall': finish(history[0])
        else: finish('remembered')
    elif method == 'test/accept':
        reply(start_request, {'turn':{'id':active,'status':'inProgress','items':[]}})
        start_request = None
        reply(frame)
    elif method == 'turn/steer':
        if params['expectedTurnId'] != active: reply(frame,error={'code':-32600,'message':'wrong turn'})
        else:
            state['steered'] = params['input'][0]['text']
            reply(frame, {'turnId':active})
    elif method == 'turn/interrupt':
        if params['turnId'] != active: reply(frame,error={'code':-32600,'message':'wrong turn'})
        else:
            reply(frame)
            finish('interrupted','interrupted')
    elif method == 'test/release':
        reply(frame)
        finish(state.pop('steered','finished'),params.get('status','completed'),params.get('error'))
    elif method == 'thread/compact/start':
        compact = 'compact-1'
        note('turn/started',turn={'id':compact,'status':'inProgress','items':[]})
        reply(frame)
        note('item/completed',turnId=compact,item={'type':'contextCompaction','id':'compact-item'})
    elif method == 'test/release-compact':
        note('turn/completed',turn={'id':compact,'status':'completed','items':[]})
        reply(frame)
    elif method == 'test/resolve-input':
        note('serverRequest/resolved',requestId=pending)
        pending = None
        reply(frame)
    elif method == 'test/crash': sys.exit(0)
    elif method == 'test/barrier': reply(frame)
    elif method is None and pending is not None and type(frame['id']) is type(pending) and frame['id'] == pending:
        if 'error' in frame: finish('cancelled')
        elif pending == 77: finish(frame['result']['answers']['choice']['answers'][0])
        else: finish(frame['result']['decision'])
        pending = None
    elif method is not None: reply(frame,error={'code':-32601,'message':'unknown method'})
'''


class Sink:
    enabled = True

    def __init__(self):
        self.events = []

    def emit(self, payload):
        self.events.append(payload)


class CodexLifecycleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cwd = self.root / 'project'
        self.cwd.mkdir()
        self.native = self.root / 'native'
        self.native.mkdir()
        command = [sys.executable,'-u','-c',PEER,str(self.native)]
        command_patch = patch.object(CodexWorker,'_command',return_value=command)
        command_patch.start()
        self.addCleanup(command_patch.stop)
        self.sink = Sink()
        self.store = Store(self.root / 'state',self.sink)
        self.addCleanup(self.store.close)
        self.session = self.store.create({'name':'codex','cwd':str(self.cwd),'options':{'backend':'codex'}})
        self.sid = self.session['id']

    def wait_for(self, predicate):
        with self.store.cv:
            self.assertTrue(self.store.cv.wait_for(predicate,timeout=10),'Native peer did not reach expected state')

    def begin(self, message):
        dispatched = threading.Event()
        run = self.store._run
        def observe_dispatch(*args):
            try: run(*args)
            finally: dispatched.set()
        with patch.object(self.store,'_run',side_effect=observe_dispatch):
            initial = self.store.turn(self.sid,message)
            self.assertTrue(dispatched.wait(10),'Delegate dispatch did not return')
        return self.store.jobs[initial['id']]

    def settled(self, job):
        self.wait_for(lambda: job['status'] in TERMINAL)
        return job

    def worker(self):
        return self.store.workers[self.sid]

    def test_two_turns_and_worker_restart_resume_same_native_history(self):
        self.settled(self.begin('private-context-token'))
        worker, thread_id = self.worker(), self.worker().thread_id
        second = self.settled(self.begin('recall'))
        self.assertEqual((second['status'],second['finalMessage']),('completed','private-context-token'))
        self.assertIs(self.worker(),worker)
        self.store.session_action(self.sid,'close',{})
        third = self.settled(self.begin('recall'))
        self.assertEqual(third['finalMessage'],'private-context-token')
        self.assertEqual(self.worker().thread_id,thread_id)
        self.assertEqual([e['event'] for e in self.sink.events],['run.done']*3)

    def test_item_completion_and_stale_turns_do_not_finish_successor(self):
        self.settled(self.begin('first'))
        job = self.begin('successor')
        self.wait_for(lambda: job['finalMessage'] == 'partial')
        self.assertEqual(job['status'],'running')
        other = self.store.create({'name':'omp','cwd':str(self.cwd)})
        with self.assertRaises(Conflict): self.store.turn(other['id'],'must not steal worktree')
        self.worker().request('test/release')
        self.assertEqual(self.settled(job)['finalMessage'],'finished')
        self.assertEqual([e['message'] for e in self.sink.events],['remembered','finished'])

    def test_question_before_start_response_requires_complete_valid_answer(self):
        job = self.begin('question')
        self.wait_for(lambda: job['status'] == 'waiting_input')
        request_id = job['pendingInput'][0]['id']
        for answers in ([],[{'id':'other','selectedOptions':['B']}],[{'id':'choice','selectedOptions':['invalid']}]):
            with self.subTest(answers=answers):
                with self.assertRaises(ValueError):
                    self.store.control(job['id'],'answer',{'id':request_id,'answers':answers})
                self.assertEqual(job['status'],'waiting_input')
        self.store.control(job['id'],'answer',{'id':request_id,'answers':[{'id':'choice','selectedOptions':['B']}]})
        self.assertEqual((self.settled(job)['status'],job['finalMessage']),('completed','B'))
        self.assertEqual([e['event'] for e in self.sink.events],['run.needs_input','run.done'])

    def test_approval_rejects_truthy_strings_and_preserves_native_request_identity(self):
        for prompt, answer, expected in [('approval',False,'decline'),('file-approval',True,'accept')]:
            with self.subTest(prompt=prompt):
                job = self.begin(prompt)
                self.wait_for(lambda: job['status'] == 'waiting_input')
                request_id = job['pendingInput'][0]['id']
                with self.assertRaises(ValueError):
                    self.store.control(job['id'],'answer',{'id':request_id,'confirmed':'false'})
                self.assertEqual(job['status'],'waiting_input')
                self.store.control(job['id'],'answer',{'id':request_id,'confirmed':answer})
                self.assertEqual(self.settled(job)['finalMessage'],expected)

    def test_native_resolution_clears_input_and_late_answers_are_rejected(self):
        job = self.begin('approval')
        self.wait_for(lambda: job['status'] == 'waiting_input')
        request_id = job['pendingInput'][0]['id']
        self.worker().request('test/resolve-input')
        self.wait_for(lambda: not job['pendingInput'])
        self.assertEqual(job['status'],'running')
        with self.assertRaises(Conflict):
            self.store.control(job['id'],'answer',{'id':request_id,'confirmed':True})
        self.worker().request('test/release')
        self.settled(job)

    def test_retry_notice_is_not_terminal_and_provider_failure_is_preserved(self):
        job = self.begin('failure')
        self.worker().request('test/barrier')
        self.assertEqual(job['status'],'running')
        self.worker().request('test/release',status='failed',error={'message':'quota exhausted'})
        self.assertEqual((self.settled(job)['status'],job['error']),('error','quota exhausted'))
        self.assertEqual(self.sink.events[-1]['event'],'run.error')

    def test_steering_waits_for_owning_turn_acceptance_and_interrupt_is_terminal(self):
        job = self.begin('blocked-start')
        entered = threading.Event()
        errors = []
        begin_control = self.worker().begin_control
        def capture_control(*args, **kwargs):
            ticket = begin_control(*args,**kwargs)
            entered.set()
            return ticket
        def steer():
            try: self.store.control(job['id'],'steer',{'message':'changed'})
            except Exception as exc: errors.append(exc)
        with patch.object(self.worker(),'begin_control',side_effect=capture_control):
            thread = threading.Thread(target=steer)
            thread.start()
            self.assertTrue(entered.wait(10))
            self.worker().request('test/accept')
            thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors,[])
        self.worker().request('test/release')
        self.assertEqual(self.settled(job)['finalMessage'],'changed')
        interrupted = self.begin('hold')
        self.store.control(interrupted['id'],'interrupt',{})
        self.assertEqual(self.settled(interrupted)['status'],'interrupted')

    def test_failed_resume_does_not_replace_or_replay_conversation(self):
        self.settled(self.begin('context'))
        identity = Path(self.session['sessionDir']) / 'codex-thread.json'
        original = identity.read_text()
        self.store.session_action(self.sid,'close',{})
        state_path = self.native / 'native.json'
        state = json.loads(state_path.read_text())
        state['threads'] = {}
        state_path.write_text(json.dumps(state))
        failed = self.settled(self.begin('must not replay'))
        self.assertEqual(failed['status'],'error')
        self.assertIn('no rollout found',failed['error'])
        self.assertEqual(identity.read_text(),original)
        self.assertEqual(json.loads(state_path.read_text())['next'],1)

    def test_prewarm_only_thread_can_reopen_without_inventing_history(self):
        self.store.session_action(self.sid,'open',{})
        first = self.worker().thread_id
        self.store.session_action(self.sid,'close',{})
        self.store.session_action(self.sid,'open',{})
        self.assertNotEqual(self.worker().thread_id,first)
        self.assertEqual(self.settled(self.begin('recall'))['finalMessage'],'recall')

    def test_compaction_keeps_worktree_reserved_until_terminal_turn(self):
        self.settled(self.begin('context'))
        entered = threading.Event()
        errors = []
        compact_worker = self.worker().compact
        def observe_compaction():
            entered.set()
            return compact_worker()
        def compact():
            try: self.store.compact(self.sid)
            except Exception as exc: errors.append(exc)
        with patch.object(self.worker(),'compact',side_effect=observe_compaction):
            thread = threading.Thread(target=compact)
            thread.start()
            self.assertTrue(entered.wait(10))
            self.worker().request('test/barrier')
            with self.assertRaises(Conflict): self.store.turn(self.sid,'must wait for compaction')
            self.worker().request('test/release-compact')
            thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors,[])
        self.assertEqual(self.settled(self.begin('recall'))['finalMessage'],'context')

    def test_interrupt_close_waits_for_native_completion_already_in_flight(self):
        job = self.begin('hold')
        worker = self.worker()
        terminal_ready = threading.Event()
        release_terminal = threading.Event()
        close_requested = threading.Event()
        callback = worker.on_event
        begin_control = worker.begin_control
        errors = []
        closer = None

        def pause_terminal(event):
            if event.get('type') == 'prompt_result':
                terminal_ready.set()
                if not release_terminal.wait(10):
                    raise RuntimeError('Terminal event release timed out')
            callback(event)

        def observe_control(*args, **kwargs):
            try:
                return begin_control(*args, **kwargs)
            finally:
                close_requested.set()

        def close():
            try:
                self.store.session_action(self.sid, 'close', {'interrupt': True})
            except Exception as exc:
                errors.append(exc)

        worker.on_event = pause_terminal
        try:
            worker.request('test/release')
            self.assertTrue(terminal_ready.wait(10))
            with patch.object(worker, 'begin_control', side_effect=observe_control):
                closer = threading.Thread(target=close)
                closer.start()
                self.assertTrue(close_requested.wait(10))
        finally:
            release_terminal.set()
            if closer is not None:
                closer.join(10)
            worker.on_event = callback

        self.assertFalse(closer.is_alive())
        self.assertEqual(self.store.show_session(self.sid)['worker']['state'], 'stopped')
        self.assertEqual(errors, [])
        self.assertEqual(self.settled(job)['status'], 'completed')
        self.assertEqual(self.settled(self.begin('recall'))['finalMessage'], 'hold')

    def test_native_process_exit_finishes_job_with_error(self):
        job = self.begin('hold')
        with self.assertRaisesRegex(RuntimeError,'exited'): self.worker().request('test/crash',timeout=10)
        self.assertEqual(self.settled(job)['status'],'error')
        self.assertEqual(self.store.show_session(self.sid)['worker']['state'],'failed')

    def test_rejected_prompt_is_error_not_success_and_worker_can_take_next_turn(self):
        job = self.settled(self.begin('reject'))
        self.assertEqual((job['status'],job['error']),('error','prompt rejected'))
        self.assertEqual(self.settled(self.begin('next'))['status'],'completed')


if __name__ == '__main__':
    unittest.main()
