import sys
import os
import re
import argparse
import tempfile
import shutil
import zipfile

from util import load_json
from color_util import cprint, colors
import bash_completion as bc
from verbose import VerbosePrinter
import tests_util as tu


BASE_DIR = os.environ.get('BASE_DIR')
TESTS_DIR = os.environ.get('TESTS_DIR')
PROBLEM_JSON = os.environ.get('PROBLEM_JSON')
SUBTASKS_JSON = os.environ.get('SUBTASKS_JSON')
SOLUTIONS_JSON = os.environ.get('SOLUTIONS_JSON')
VALIDATOR_DIR = os.environ.get('VALIDATOR_DIR')
CHECKER_DIR = os.environ.get('CHECKER_DIR')
SOLUTION_DIR = os.environ.get('SOLUTION_DIR')
GEN_DIR = os.environ.get('GEN_DIR')
STATEMENT_DIR = os.environ.get('STATEMENT_DIR')

warnings = []
vp = VerbosePrinter()


def warn(message):
    warnings.append(message)
    cprint(colors.WARN, message)


class ExportFailureException(Exception):
    pass


def read_text_lf(path):
    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        return f.read().replace('\r\n', '\n')


def write_text_lf(path, content):
    with open(path, 'w', encoding='utf-8', newline='\n') as f:
        f.write(content.replace('\r\n', '\n'))


def copy_lf_file(src, dst):
    """Copy text data file normalizing CRLF to LF for Linux UOJ judgers."""
    with open(src, 'rb') as f:
        raw = f.read()
    raw = raw.replace(b'\r\n', b'\n')
    with open(dst, 'wb') as f:
        f.write(raw)


def find_model_solution():
    if not SOLUTIONS_JSON or not os.path.isfile(SOLUTIONS_JSON):
        return None
    solutions = load_json(SOLUTIONS_JSON)
    for sol_name, info in solutions.items():
        if info.get('verdict') == 'model_solution':
            p = os.path.join(SOLUTION_DIR, sol_name)
            if os.path.isfile(p):
                return p
    return None


def collect_tests():
    """Return (sample_tests, main_tests, subtask_blocks) from subtasks.json and tests/mapping."""
    if not TESTS_DIR or not os.path.isdir(TESTS_DIR):
        raise ExportFailureException("Tests directory not found: '{}'. Run 'tps gen' first.".format(TESTS_DIR))

    subtasks_data = load_json(SUBTASKS_JSON).get('subtasks', {})
    subtask_tests = tu.get_subtasks_tests_dict_from_tests_dir(TESTS_DIR)

    ordered_subtasks = sorted(
        subtasks_data.items(),
        key=lambda item: item[1].get('index', 0),
    )

    sample_tests = []
    main_tests = []
    subtask_blocks = []

    for st_name, st_info in ordered_subtasks:
        tlist = subtask_tests.get(st_name, [])
        score = int(st_info.get('score', 0))
        if st_name == 'samples' or score == 0:
            for t in tlist:
                if t not in sample_tests:
                    sample_tests.append(t)
        else:
            if not tlist:
                continue
            start_idx = len(main_tests) + 1
            main_tests.extend(tlist)
            end_idx = len(main_tests)
            subtask_blocks.append({
                'name': st_name,
                'score': score,
                'start': start_idx,
                'end': end_idx,
            })

    if not main_tests:
        raise ExportFailureException("No non-zero subtask tests found in '{}'.".format(TESTS_DIR))

    return sample_tests, main_tests, subtask_blocks


def build_problem_conf(task_data, n_tests, n_ex_tests, subtask_blocks, checker_name, custom_checker):
    """Build UOJ/TunaOJ problem.conf matching UOJProblemDataSynchronizer & UOJAIProblemStudio."""
    time_limit = max(1, int(round(float(task_data.get('time_limit', 1)))))
    memory_limit = max(32, int(round(float(task_data.get('memory_limit', 256)))))

    lines = [
        "use_builtin_judger on",
    ]
    if not custom_checker:
        lines.append("use_builtin_checker {}".format(checker_name))

    lines.extend([
        "n_tests {}".format(n_tests),
        "n_ex_tests {}".format(n_ex_tests),
        "n_sample_tests {}".format(n_ex_tests),
    ])

    if len(subtask_blocks) > 1:
        lines.append("n_subtasks {}".format(len(subtask_blocks)))
        for idx, blk in enumerate(subtask_blocks, start=1):
            lines.append("subtask_end_{} {}".format(idx, blk['end']))
            lines.append("subtask_score_{} {}".format(idx, blk['score']))

    lines.extend([
        "input_pre ",
        "input_suf in",
        "output_pre ",
        "output_suf out",
        "time_limit {}".format(time_limit),
        "memory_limit {}".format(memory_limit),
    ])
    return "\n".join(lines) + "\n"


def parse_tps_statement_sections(md_text):
    """Extract sections from TPS statement/index.md if available."""
    sections = {'intro': '', 'details': '', 'constraints': '', 'subtasks': ''}
    if not md_text:
        return sections

    lines = md_text.splitlines()
    current = 'intro'
    buf = []

    def flush(key, content_lines):
        text = "\n".join(content_lines).strip()
        if key in sections and text:
            sections[key] = text

    for line in lines:
        if line.startswith('# ') and current == 'intro' and not buf:
            continue
        m = re.match(r'^##\s+(.+?)\s*$', line)
        if m:
            flush(current, buf)
            buf = []
            heading = m.group(1).lower()
            if 'implementation' in heading or 'input' in heading or 'format' in heading:
                current = 'details'
            elif 'constraint' in heading:
                current = 'constraints'
            elif 'subtask' in heading:
                current = 'subtasks'
            else:
                current = 'other'
        else:
            buf.append(line)

    flush(current, buf)
    return sections


def build_tunaoj_statement(task_data, sample_pairs, subtask_blocks):
    """Generate TunaOJ-formatted Markdown statement (following UOJAIProblemStudio format)."""
    title = task_data.get('title') or task_data.get('name') or 'Problem'
    time_limit = max(1, int(round(float(task_data.get('time_limit', 1)))))
    memory_limit = max(32, int(round(float(task_data.get('memory_limit', 256)))))

    tps_md_path = os.path.join(STATEMENT_DIR, 'index.md') if STATEMENT_DIR else ''
    sections = parse_tps_statement_sections(read_text_lf(tps_md_path) if os.path.isfile(tps_md_path) else '')

    intro = sections['intro'] or (task_data.get('description') or '请在此处补充题目描述。')
    details = sections['details'] or '请在此处补充输入输出格式说明。'
    constraints = sections['constraints']

    out = [
        "# {}".format(title),
        "",
        "### 题目描述",
        intro,
        "",
        "### 输入格式",
        details,
        "",
        "### 输出格式",
        "输出对应的答案。",
        "",
    ]

    for idx, (in_txt, out_txt) in enumerate(sample_pairs, start=1):
        out.extend([
            "### 样例 {}".format(idx),
            "#### 样例输入 {}".format(idx),
            "```",
            in_txt.rstrip('\n'),
            "```",
            "#### 样例输出 {}".format(idx),
            "```",
            out_txt.rstrip('\n'),
            "```",
            "#### 样例解释 {}".format(idx),
            "请在此处补充样例 {} 解释。".format(idx),
            "",
        ])

    out.append("### 数据范围与提示")
    if constraints:
        out.append(constraints)
        out.append("")
    if sections['subtasks']:
        out.append(sections['subtasks'])
        out.append("")
    else:
        for idx, blk in enumerate(subtask_blocks, start=1):
            out.append("- Subtask {}（{}）：{} 分（测试点 {} ~ {}）".format(
                idx, blk['name'], blk['score'], blk['start'], blk['end']
            ))
        out.append("")

    out.append("- 时间限制：`{} s`，空间限制：`{} MB`".format(time_limit, memory_limit))
    return "\n".join(out) + "\n"


def export(output_name, checker_name='wcmp', custom_checker=False, include_sources=True):
    task_data = load_json(PROBLEM_JSON)
    sample_tests, main_tests, subtask_blocks = collect_tests()

    with tempfile.TemporaryDirectory() as tmpdir:
        # 1. Copy main tests as 1.in / 1.out .. N.in / N.out
        for idx, test_name in enumerate(main_tests, start=1):
            src_in = os.path.join(TESTS_DIR, "{}.in".format(test_name))
            src_out = os.path.join(TESTS_DIR, "{}.out".format(test_name))
            if not os.path.isfile(src_in) or not os.path.isfile(src_out):
                raise ExportFailureException("Missing test files for '{}' in '{}'.".format(test_name, TESTS_DIR))
            copy_lf_file(src_in, os.path.join(tmpdir, "{}.in".format(idx)))
            copy_lf_file(src_out, os.path.join(tmpdir, "{}.out".format(idx)))

        # 2. Copy sample tests as ex_1.in / ex_1.out .. ex_M.in / ex_M.out
        sample_pairs = []
        for idx, test_name in enumerate(sample_tests, start=1):
            src_in = os.path.join(TESTS_DIR, "{}.in".format(test_name))
            src_out = os.path.join(TESTS_DIR, "{}.out".format(test_name))
            if not os.path.isfile(src_in) or not os.path.isfile(src_out):
                raise ExportFailureException("Missing sample test files for '{}'.".format(test_name))
            copy_lf_file(src_in, os.path.join(tmpdir, "ex_{}.in".format(idx)))
            copy_lf_file(src_out, os.path.join(tmpdir, "ex_{}.out".format(idx)))
            sample_pairs.append((read_text_lf(src_in), read_text_lf(src_out)))

        # 3. Optionally pack std.cpp, val.cpp, chk.cpp, testlib.h
        if include_sources:
            testlib_src = os.path.join(GEN_DIR, 'testlib.h') if GEN_DIR else ''
            if testlib_src and os.path.isfile(testlib_src):
                copy_lf_file(testlib_src, os.path.join(tmpdir, 'testlib.h'))

            val_src = os.path.join(VALIDATOR_DIR, 'validator.cpp') if VALIDATOR_DIR else ''
            if val_src and os.path.isfile(val_src):
                copy_lf_file(val_src, os.path.join(tmpdir, 'val.cpp'))

            std_src = find_model_solution()
            if std_src:
                copy_lf_file(std_src, os.path.join(tmpdir, 'std.cpp'))

        if custom_checker:
            chk_src = os.path.join(CHECKER_DIR, 'checker.cpp') if CHECKER_DIR else ''
            if not chk_src or not os.path.isfile(chk_src):
                raise ExportFailureException("Custom checker requested, but '{}' not found.".format(chk_src))
            copy_lf_file(chk_src, os.path.join(tmpdir, 'chk.cpp'))
            testlib_src = os.path.join(GEN_DIR, 'testlib.h') if GEN_DIR else ''
            if testlib_src and os.path.isfile(testlib_src):
                copy_lf_file(testlib_src, os.path.join(tmpdir, 'testlib.h'))

        # 4. Write problem.conf
        conf_text = build_problem_conf(
            task_data,
            n_tests=len(main_tests),
            n_ex_tests=len(sample_tests),
            subtask_blocks=subtask_blocks,
            checker_name=checker_name,
            custom_checker=custom_checker,
        )
        write_text_lf(os.path.join(tmpdir, 'problem.conf'), conf_text)

        # 5. Create flat zip archive in BASE_DIR
        zip_path = os.path.join(BASE_DIR, "{}-tunaoj.zip".format(output_name))
        with zipfile.ZipFile(zip_path, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
            for fname in sorted(os.listdir(tmpdir)):
                fpath = os.path.join(tmpdir, fname)
                if os.path.isfile(fpath):
                    zf.write(fpath, arcname=fname)

        # 6. Write TunaOJ Markdown statement in BASE_DIR
        stmt_path = os.path.join(BASE_DIR, "{}-tunaoj-statement.md".format(output_name))
        write_text_lf(stmt_path, build_tunaoj_statement(task_data, sample_pairs, subtask_blocks))

        return zip_path, stmt_path


def bash_completion_list(argv):
    current_token_info = bc.extract_current_token_info(argv)
    return bc.simple_argument_completion(
        current_token_info=current_token_info,
        available_options=[
            "--help",
            "--verbose",
            "--output-name=",
            "--checker=",
            "--custom-checker",
            "--no-sources",
        ],
        enable_file_completion=False,
        option_value_completion_functions={
            ("-o", "--output-name"): bc.empty_completion_function,
            ("--checker",): bc.empty_completion_function,
        },
    )


def main():
    if len(sys.argv) > 1 and sys.argv[1] == '--bash-completion':
        sys.argv.pop(1)
        bc.print_all(bash_completion_list(sys.argv))
        sys.exit(0)

    parser = argparse.ArgumentParser(
        prog="tps export TunaOJ",
        description="Exporter for TunaOJ (UOJ-based problem data zip & Markdown statement).",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Prints verbose details.",
    )
    parser.add_argument(
        "-o", "--output-name",
        metavar="<export-output-name>",
        help="Base name for the exported zip and statement markdown.",
    )
    parser.add_argument(
        "--checker",
        default="wcmp",
        help="Built-in UOJ checker name (default: wcmp, e.g. ncmp, wcmp, fcmp, lcmp).",
    )
    parser.add_argument(
        "--custom-checker",
        action="store_true",
        help="Pack checker/checker.cpp as chk.cpp instead of using a built-in checker.",
    )
    parser.add_argument(
        "--no-sources",
        action="store_true",
        help="Do not pack std.cpp / val.cpp / testlib.h into the zip.",
    )
    args = parser.parse_args()

    vp.enabled = args.verbose
    task_data = load_json(PROBLEM_JSON)
    output_name = args.output_name if args.output_name else task_data['name']

    try:
        zip_path, stmt_path = export(
            output_name=output_name,
            checker_name=args.checker,
            custom_checker=args.custom_checker,
            include_sources=not args.no_sources,
        )
        cprint(colors.SUCCESS, "Successfully exported TunaOJ data zip to '{}'.".format(zip_path))
        cprint(colors.SUCCESS, "Successfully exported TunaOJ statement to '{}'.".format(stmt_path))
    except ExportFailureException as e:
        cprint(colors.FAIL, "Exporting failed: {}".format(e))
        sys.exit(1)


if __name__ == '__main__':
    main()
