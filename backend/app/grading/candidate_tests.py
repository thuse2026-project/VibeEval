"""Untrusted candidate-test observations, never an automatic score."""

import ast
import hashlib
import socket

from app.grading.datasette import CandidateFailure, evaluate

TEST_PATH = "tests/test_candidate.py"
WORKER = r"""
import contextlib,io,json,sys,traceback,unittest
sys.path[:0]=['/workspace','/workspace/tests']
class Capture(io.StringIO):
    def __init__(self):super().__init__();self.truncated=False
    def write(self,text):
        remaining=max(0,8000-self.tell());self.truncated|=len(text)>remaining
        super().write(text[:remaining]);return len(text)
class Result(unittest.TestResult):
    def __init__(self):super().__init__();self.rows={};self.rows_truncated=False
    def record(self,test,status,message=''):
        key=test.id()[:500]
        if len(self.rows)<200 or key in self.rows:
            self.rows[key]={'id':key,'status':status,'message':message[:1000],'truncated':len(message)>1000}
        else:self.rows_truncated=True
    def addSuccess(self,test):
        if test.id() not in self.rows:self.record(test,'passed')
    def addFailure(self,test,err):self.record(test,'failed',self._exc_info_to_string(err,test))
    def addError(self,test,err):self.record(test,'error',self._exc_info_to_string(err,test))
    def addSkip(self,test,reason):self.record(test,'skipped',reason)
    def addExpectedFailure(self,test,err):self.record(test,'expected_failure',self._exc_info_to_string(err,test))
    def addUnexpectedSuccess(self,test):self.record(test,'unexpected_success')
    def addSubTest(self,test,subtest,err):
        if err is not None:
            self.record(subtest,'failed' if issubclass(err[0],test.failureException) else 'error',self._exc_info_to_string(err,test))
            self.record(test,'failed' if issubclass(err[0],test.failureException) else 'error','包含未通过的子测试；请展开对应记录。')
for line in sys.stdin:
    request=json.loads(line)
    if request['op']=='health':
        value={'protocol':1,'python':list(sys.version_info[:2])}
    else:
        output=Capture();result=Result()
        with contextlib.redirect_stdout(output),contextlib.redirect_stderr(output):
            try:
                suite=unittest.defaultTestLoader.loadTestsFromName('test_candidate')
                if suite.countTestCases()>200:
                    result.rows_truncated=True;raise ValueError('最多观察200项新增测试，未完整运行')
                suite.run(result)
            except BaseException:
                message=traceback.format_exc()
                result.rows['test_candidate']={'id':'test_candidate','status':'error','message':message[-1000:],'truncated':len(message)>1000}
        value={'tests':list(result.rows.values()),'tests_truncated':result.rows_truncated,'output':output.getvalue(),'output_truncated':output.truncated}
    print(json.dumps({'id':request['id'],'value':value}),flush=True)
"""


def possible_copies(source, public):
    """Syntax-only hint. Different fixtures can change meaning; examiner decides."""

    def bodies(text):
        found = {}
        for node in ast.walk(ast.parse(text)):
            if isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef)
            ) and node.name.startswith("test"):
                body = list(node.body)
                if (
                    body
                    and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)
                ):
                    body.pop(0)
                found[node.name] = ast.dump(
                    ast.Module(body=body, type_ignores=[]), include_attributes=False
                )
        return found

    if max(len(source), len(public)) > 200000:
        return []
    try:
        known = set(bodies(public).values())
        return sorted(name for name, body in bodies(source).items() if body in known)
    except (SyntaxError, RecursionError, ValueError):
        return []


def observe(channel):
    # Longer than ordinary HTTP probes, still bounded independently of test code.
    channel.socket.settimeout(60)
    try:
        value = channel.call("candidate_tests", "candidate")
    except CandidateFailure as error:
        timeout = isinstance(error.__cause__, (TimeoutError, socket.timeout))
        return {
            "status": "timed_out" if timeout else "unconfirmed",
            "tests": [],
            "output": "",
            "message": (
                "运行超时，不能据此判测试质量零分。"
                if timeout
                else "测试进程退出或响应未能确认，请核查后重试；不自动扣分。"
            ),
        }
    unconfirmed = {
        "status": "unconfirmed",
        "tests": [],
        "output": "",
        "message": "考生测试返回的运行记录无效或不完整，须核查；不自动扣分。",
    }
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("tests"), list)
        or len(value["tests"]) > 200
    ):
        return unconfirmed
    allowed = {
        "passed",
        "failed",
        "error",
        "skipped",
        "expected_failure",
        "unexpected_success",
    }
    rows = []
    for row in value["tests"]:
        if (
            not isinstance(row, dict)
            or row.get("status") not in allowed
            or not isinstance(row.get("id"), str)
        ):
            return unconfirmed
        rows.append(
            {
                "id": row["id"][:500],
                "status": row["status"],
                "message": str(row.get("message", ""))[:3000],
                "truncated": bool(row.get("truncated", False)),
            }
        )
    if len({row["id"] for row in rows}) != len(rows):
        return unconfirmed
    return {
        "status": "truncated" if value.get("tests_truncated") else "completed",
        "tests": rows,
        "tests_truncated": bool(value.get("tests_truncated", False)),
        "output": str(value.get("output", ""))[:8000],
        "output_truncated": bool(value.get("output_truncated", False)),
    }


def compare_tests(files, rule, image, docker_url):
    submitted = {f["path"]: f["content"] for f in files}
    source = submitted.get(TEST_PATH)
    if source is None or TEST_PATH not in rule["writable"]:
        raise ValueError("candidate test file is not published and writable")
    copies = possible_copies(source, rule["baseline"].get("tests/test_public.py", ""))
    reports = {}
    for version in ("original", "submitted"):
        workspace = dict(rule["baseline"]) if version == "original" else dict(submitted)
        workspace[TEST_PATH] = source
        reports[version] = evaluate(
            [{"path": p, "content": text} for p, text in workspace.items()],
            rule,
            image,
            docker_url,
            runner=observe,
            worker=WORKER,
        )
    rows = {}
    for version, report in reports.items():
        for test in report.get("tests", []):
            row = rows.setdefault(
                test["id"],
                {
                    "id": test["id"],
                    "original": None,
                    "submitted": None,
                    "possible_public_copy": test["id"].rsplit(".", 1)[-1] in copies,
                },
            )
            row[version] = {
                "status": test["status"],
                "message": test["message"],
                "truncated": test.get("truncated", False),
            }
    return {
        "status": (
            "completed"
            if all(r["status"] == "completed" for r in reports.values())
            else "incomplete"
        ),
        "test_sha256": hashlib.sha256(source.encode()).hexdigest(),
        "versions": reports,
        "rows": list(rows.values()),
        "possible_copy_methods": copies,
        "notice": "考生测试及其输出是辅助证据，不是可信评分；不按通过数量给分。复制提示须结合夹具和代码人工核对。",
    }
