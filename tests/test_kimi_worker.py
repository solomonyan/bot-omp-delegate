"""Exercise Kimi ACP through the real Store and stdio adapter with a local peer."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from bot_omp_delegate.kimi_worker import KimiWorker
from bot_omp_delegate.lifecycle import Conflict, Store, TERMINAL


PEER = r'''
import json, pathlib, sys
root = pathlib.Path(sys.argv[1])
path = root / 'peer.json'
state = json.loads(path.read_text()) if path.exists() else {'next': 0, 'sessions': {}, 'frames': []}
session = pending = prompt = None

def save(): path.write_text(json.dumps(state))
def send(frame): print(json.dumps(frame), flush=True)
def reply(request, result=None, error=None):
    send({'jsonrpc':'2.0', 'id':request['id'], **({'error':error} if error else {'result':result or {}})})
def update(kind, **fields):
    send({'jsonrpc':'2.0','method':'session/update','params':{'sessionId':session,'update':{'sessionUpdate':kind,**fields}}})
def finish(reason='end_turn'):
    global prompt, pending
    if prompt is None: return
    send({'jsonrpc':'2.0','id':prompt['id'],'result':{'stopReason':reason}})
    prompt = None

def snapshot(): return {'frames':state['frames'], 'sessions':state['sessions'], 'pending':pending, 'prompt':prompt is not None}

for line in sys.stdin:
    frame = json.loads(line)
    method, params = frame.get('method'), frame.get('params', {})
    state['frames'].append(frame); save()
    if method == 'initialize':
        reply(frame, {'protocolVersion':1,'agentCapabilities':{'loadSession':True,'sessionCapabilities':{'resume':{}}},'agentInfo':{'name':'peer','version':'1'}})
    elif method == 'session/new':
        state['next'] += 1; session = 'kimi-' + str(state['next'])
        state['sessions'][session] = []
        reply(frame, {'sessionId':session,'configOptions':[],'modes':[]}); save()
    elif method == 'session/resume':
        session = params['sessionId']
        if session not in state['sessions']:
            reply(frame, error={'code':-32000,'message':'resume unavailable'})
        else:
            # ACP replay before resume response must never become the current job output.
            update('agent_message_chunk', messageId='replay', content={'type':'text','text':'old transcript'})
            reply(frame, {'sessionId':session,'configOptions':[],'modes':[]})
    elif method == 'session/set_config_option':
        reply(frame, {'configOptions':[{'id':params['configId'],'name':params['configId'],'type':'select','currentValue':params['value'],'options':[{'value':params['value'],'name':params['value']}]}]})
    elif method == 'session/prompt':
        prompt = frame
        text = params['prompt'][0]['text']
        state['sessions'][session].append(text); save()
        if text == 'streams':
            update('agent_thought_chunk', content={'type':'text','text':'private reasoning'})
            update('agent_message_chunk', messageId='m1', content={'type':'text','text':'first '})
            update('agent_message_chunk', messageId='m1', content={'type':'text','text':'message'})
            update('tool_call', toolCallId='tool-1', title='run', status='pending')
            update('tool_call_update', toolCallId='tool-1', status='completed', content=[{'type':'content','content':{'type':'text','text':'tool output'}}])
            update('agent_message_chunk', messageId='m2', content={'type':'text','text':'answer'})
        elif text == 'interleaved':
            update('tool_call', toolCallId='background-tool', title='run', status='pending')
            update('agent_message_chunk', content={'type':'text','text':'Complete '})
            update('tool_call_update', toolCallId='background-tool', status='completed', content=[{'type':'content','content':{'type':'text','text':'late tool output'}}])
            update('agent_message_chunk', content={'type':'text','text':'answer'})
            finish()
        elif text == 'permission-numeric':
            pending = 17
            send({'jsonrpc':'2.0','id':pending,'method':'session/request_permission','params':{'sessionId':session,'toolCall':{'toolCallId':'t','title':'Run'},'options':[{'optionId':'persist','name':'Always allow','kind':'allow_always'},{'optionId':'once','name':'Allow once','kind':'allow_once'},{'optionId':'no','name':'Reject','kind':'reject_once'}]}})
        elif text == 'permission-string':
            pending = 'permission-18'
            send({'jsonrpc':'2.0','id':pending,'method':'session/request_permission','params':{'sessionId':session,'toolCall':{'toolCallId':'t','title':'Run'},'options':[{'optionId':'once','name':'Allow once','kind':'allow_once'},{'optionId':'no','name':'Reject','kind':'reject_once'}]}})
        elif text == 'form':
            pending = 29
            send({'jsonrpc':'2.0','id':pending,'method':'elicitation/create','params':{'sessionId':session,'toolCallId':'ask','mode':'form','message':'Choose settings','requestedSchema':{'type':'object','properties':{'q0':{'type':'string','title':'Target','oneOf':[{'const':'A','title':'Alpha'},{'const':'B','title':'Beta'}]},'q1':{'type':'array','title':'Features','minItems':1,'items':{'anyOf':[{'const':'x','title':'X'},{'const':'y','title':'Y'}]}}},'required':['q0','q1']}}})
        elif text in ('hold','cancel'):
            update('agent_message_chunk', messageId='m', content={'type':'text','text':'pending'})
        elif text == 'limits': finish('max_turn_requests')
        elif text == 'refusal': finish('refusal')
        elif text == 'error':
            send({'jsonrpc':'2.0','id':frame['id'],'error':{'code':-32001,'message':'provider unavailable'}}); prompt = None
        elif text == 'cancelled': finish('cancelled')
        elif text == 'stale':
            send({'jsonrpc':'2.0','method':'session/update','params':{'sessionId':'old-session','update':{'sessionUpdate':'agent_message_chunk','messageId':'old','content':{'type':'text','text':'wrong'}}}})
            send({'jsonrpc':'2.0','method':'session/update','params':{'sessionId':'unrelated','update':{'sessionUpdate':'agent_message_chunk','messageId':'other','content':{'type':'text','text':'wrong'}}}})
            update('agent_message_chunk', messageId='live', content={'type':'text','text':'valid'})
            finish()
        elif text == 'recall':
            update('agent_message_chunk', messageId='recall', content={'type':'text','text':'|'.join(state['sessions'][session])})
            finish()
        else: finish()
    elif method == 'session/cancel':
        state['cancelRequested'] = True
        save()
    elif method == 'test/release':
        reply(frame, {})
        finish('cancelled' if params.get('cancelled') else 'end_turn')
    elif method == 'test/state': reply(frame, snapshot())
    elif method == 'test/forget-session':
        state['sessions'].clear(); save(); reply(frame, {})
    elif method == 'test/barrier': reply(frame, {})
    elif method == 'test/crash': sys.exit(0)
    elif frame.get('id') == pending and 'method' not in frame:
        response = frame
        if response.get('result',{}).get('outcome',{}).get('outcome') == 'cancelled' or response.get('result',{}).get('action') == 'cancel':
            pending = None
            continue
        if response['id'] == 17 or response['id'] == 'permission-18':
            selected = response.get('result',{}).get('outcome',{}).get('optionId')
            update('agent_message_chunk', messageId='permission-answer', content={'type':'text','text':str(selected)})
        elif response['id'] == 29:
            content = response.get('result',{}).get('content',{})
            update('agent_message_chunk', messageId='form-answer', content={'type':'text','text':json.dumps(content,sort_keys=True)})
        pending = None
        finish()
    elif method is not None: reply(frame, error={'code':-32601,'message':'unknown method'})
'''


class Sink:
    enabled = True

    def __init__(self):
        self.events = []

    def emit(self, payload, coordinator_subscriber_id=None):
        self.events.append(payload)


class KimiWorkerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cwd = self.root / 'project'
        self.cwd.mkdir()
        self.native = self.root / 'native'
        self.native.mkdir()
        command_patch = patch.object(KimiWorker, '_command', return_value=[sys.executable, '-u', '-c', PEER, str(self.native)])
        command_patch.start()
        self.addCleanup(command_patch.stop)
        self.sink = Sink()
        self.store = Store(self.root / 'state', self.sink)
        self.addCleanup(self.store.close)
        self.session = self.store.create({'name':'kimi','cwd':str(self.cwd),'options':{'backend':'kimi'}})
        self.sid = self.session['id']

    def wait_for(self, predicate):
        with self.store.cv:
            self.assertTrue(self.store.cv.wait_for(predicate, timeout=10), 'Native peer did not reach expected state')

    def begin(self, message):
        dispatched = threading.Event()
        run = self.store._run
        def observe_dispatch(*args):
            try: run(*args)
            finally: dispatched.set()
        with patch.object(self.store, '_run', side_effect=observe_dispatch):
            initial = self.store.turn(self.sid, message)
            self.assertTrue(dispatched.wait(10), 'Delegate dispatch did not return')
        return self.store.jobs[initial['id']]

    def settled(self, job):
        self.wait_for(lambda: job['status'] in TERMINAL)
        return job

    def worker(self):
        return self.store.workers[self.sid]

    def peer_state(self):
        return self.worker().request('test/state', timeout=10)

    def test_resident_followup_and_restart_resume_exact_native_history(self):
        self.settled(self.begin('alpha'))
        worker = self.worker()
        self.assertEqual(self.settled(self.begin('beta'))['status'], 'completed')
        self.assertIs(self.worker(), worker)
        self.store.close()
        self.store = Store(self.root / 'state', self.sink)
        self.addCleanup(self.store.close)
        recalled = self.settled(self.begin('recall'))
        self.assertEqual(recalled['finalMessage'], 'alpha|beta|recall')

    def test_failed_resume_does_not_create_replacement_or_replay_prompt(self):
        self.settled(self.begin('kept'))
        identity = Path(self.session['sessionDir']) / 'kimi-session.json'
        original = identity.read_text()
        # Remove only the native conversation, leaving persisted identity untouched.
        self.worker().request('test/forget-session')
        self.store.session_action(self.sid, 'close', {})
        failed = self.settled(self.begin('must-not-replay'))
        self.assertEqual(failed['status'], 'error')
        self.assertIn('resume unavailable', failed['error'])
        peer_state = json.loads((self.native / 'peer.json').read_text())
        self.assertEqual(peer_state['next'], 1)
        self.assertFalse(any(frame.get('method') == 'session/prompt' and frame.get('params', {}).get('prompt', [{}])[0].get('text') == 'must-not-replay' for frame in peer_state['frames']))

    def test_resume_replay_and_reasoning_are_not_final_output_and_tools_are_nonterminal(self):
        self.settled(self.begin('before'))
        self.store.session_action(self.sid, 'close', {})
        job = self.begin('streams')
        self.worker().request('test/barrier')
        self.assertEqual(job['status'], 'running')
        self.assertNotIn('private reasoning', job['finalMessage'])
        self.assertNotIn('old transcript', job['finalMessage'])
        self.worker().request('test/release')
        done = self.settled(job)
        self.assertEqual((done['status'], done['finalMessage']), ('completed', 'answer'))
        self.assertNotIn('tool output', done['finalMessage'])

    def test_tool_progress_does_not_split_a_streamed_assistant_answer(self):
        job = self.settled(self.begin('interleaved'))
        self.assertEqual(job['finalMessage'], 'Complete answer')
        self.assertEqual(self.sink.events[-1]['message'], 'Complete answer')

    def test_prompt_stop_reasons_and_unrelated_session_updates(self):
        for text, status in [('limits', 'error'), ('refusal', 'completed'), ('cancelled', 'interrupted')]:
            with self.subTest(text=text):
                job = self.settled(self.begin(text))
                self.assertEqual(job['status'], status)
                if text == 'limits': self.assertIn('max_turn_requests', job['error'])
        failed = self.settled(self.begin('error'))
        self.assertEqual(failed['status'], 'error')
        self.assertIn('provider unavailable', failed['error'])
        stale = self.settled(self.begin('stale'))
        self.assertEqual(stale['finalMessage'], 'valid')

    def test_permission_keeps_exact_rpc_ids_and_only_accepts_exact_once_option(self):
        for text, expected_id in [('permission-numeric', 'once'), ('permission-string', 'once')]:
            with self.subTest(text=text):
                job = self.begin(text)
                self.wait_for(lambda: bool(job['pendingInput']))
                ui_id = job['pendingInput'][0]['id']
                for answer in ({'confirmed': 'false'}, {'value': 'unknown'}):
                    with self.assertRaises(ValueError):
                        self.store.control(job['id'], 'answer', {'id':ui_id, **answer})
                    self.assertEqual(job['status'], 'waiting_input')
                self.store.control(job['id'], 'answer', {'id':ui_id, 'value':expected_id})
                self.assertEqual(self.settled(job)['finalMessage'], expected_id)

    def test_form_elicitation_requires_complete_exact_multiselect_answers(self):
        job = self.begin('form')
        self.wait_for(lambda: bool(job['pendingInput']))
        ui_id = job['pendingInput'][0]['id']
        invalid = [[], [{'id':'q0','selectedOptions':['A']}], [{'id':'q0','selectedOptions':['wrong']},{'id':'q1','selectedOptions':['x']}], [{'id':'q0','selectedOptions':['A']},{'id':'q1','selectedOptions':['x','x']}]]
        for answers in invalid:
            with self.subTest(answers=answers):
                with self.assertRaises(ValueError):
                    self.store.control(job['id'], 'answer', {'id':ui_id, 'answers':answers})
                self.assertEqual(job['status'], 'waiting_input')
        self.store.control(job['id'], 'answer', {'id':ui_id, 'answers':[{'id':'q0','selectedOptions':['B']},{'id':'q1','selectedOptions':['x','y']}]})
        self.assertEqual(self.settled(job)['finalMessage'], '{"q0": "B", "q1": ["x", "y"]}')

    def test_cancel_notifies_peer_cancels_pending_input_and_waits_for_prompt_terminal(self):
        job = self.begin('permission-string')
        self.wait_for(lambda: bool(job['pendingInput']))
        finished = threading.Event()
        errors = []
        def cancel():
            try: self.store.control(job['id'], 'interrupt', {})
            except Exception as exc: errors.append(exc)
            finally: finished.set()
        thread = threading.Thread(target=cancel)
        thread.start()
        self.wait_for(lambda: job['status'] == 'running' and not job['pendingInput'])
        self.assertFalse(finished.is_set())
        self.assertEqual(job['status'], 'running')
        self.assertEqual(job['pendingInput'], [])
        frames = self.peer_state()['frames']
        self.assertTrue(any(frame.get('method') == 'session/cancel' for frame in frames))
        self.assertIn({'jsonrpc':'2.0','id':'permission-18','result':{'outcome':{'outcome':'cancelled'}}}, frames)
        self.worker().request('test/release', cancelled=True)
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.settled(job)['status'], 'interrupted')

    def test_interrupt_close_waits_for_terminal_callback_after_native_prompt_settles(self):
        job = self.begin('hold')
        worker = self.worker()
        terminal_ready = threading.Event()
        release_terminal = threading.Event()
        close_control_attempted = threading.Event()
        errors = []
        callback = worker.on_event
        begin_control = worker.begin_control

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
                close_control_attempted.set()

        def close():
            try:
                self.store.session_action(self.sid, 'close', {'interrupt':True})
            except Exception as exc:
                errors.append(exc)

        worker.on_event = pause_terminal
        closer = None
        try:
            worker.request('test/release')
            self.assertTrue(terminal_ready.wait(10))
            with patch.object(worker, 'begin_control', side_effect=observe_control):
                closer = threading.Thread(target=close)
                closer.start()
                self.assertTrue(close_control_attempted.wait(10))
                self.assertTrue(closer.is_alive())
        finally:
            release_terminal.set()
            if closer is not None:
                closer.join(10)
            worker.on_event = callback

        self.assertFalse(closer.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.settled(job)['status'], 'completed')
        self.assertEqual(self.store.show_session(self.sid)['worker']['state'], 'stopped')

    def test_unsupported_controls_and_transport_exit_are_not_reported_as_success(self):
        held = self.begin('hold')
        with self.assertRaisesRegex(ValueError, 'steer|unsupported|Kimi'):
            self.store.control(held['id'], 'steer', {'message':'queued work'})
        with self.assertRaisesRegex(ValueError, 'subagent|unsupported|Kimi'):
            self.store.agent_action(held['id'], 'list', {})
        self.worker().request('test/release')
        self.assertEqual(self.settled(held)['status'], 'completed')
        with self.assertRaisesRegex(ValueError, 'compact|unsupported|Kimi'):
            self.store.compact(self.sid)
        crashed = self.begin('hold')
        with self.assertRaisesRegex(RuntimeError, 'exited'):
            self.worker().request('test/crash', timeout=10)
        self.assertEqual(self.settled(crashed)['status'], 'error')


if __name__ == '__main__':
    unittest.main()
