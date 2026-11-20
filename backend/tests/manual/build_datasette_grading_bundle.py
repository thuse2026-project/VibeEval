"""Provision private grading material, never copy it into a candidate image."""

import argparse
import hashlib
import json
import re
from pathlib import Path

from app.grading.rubrics import manual_rubric

TASKS = {
    "column-statistics": {
        "public_class": "ColumnStatistics",
        "private_class": "ColumnStatisticsAcceptance",
        "public": [
            (
                "basic",
                "基本统计入口与公开场景",
                "test_filtered_page_exposes_statistics_context",
                10,
            )
        ],
        "private": [
            (
                "schema",
                "列顺序、类型、rowid、生成列和隐藏列",
                "test_schema_includes_generated_and_ui_hidden_columns",
                15,
            ),
            (
                "filter",
                "业务筛选的完整统计范围和参数类型",
                "test_filter_contract_drives_statistics_across_all_matching_rows",
                15,
            ),
            (
                "permission",
                "查询权限与统计入口保护",
                "test_does_not_expose_statistics_to_users_without_sql_permission",
                10,
            ),
            (
                "regression",
                "既有分页与业务筛选不回归",
                "test_existing_table_pagination_and_filter_still_work",
                20,
            ),
        ],
    },
    "facet-pagination": {
        "public_class": "FacetPagination",
        "private_class": "FacetAcceptance",
        "public": [
            (
                "counts",
                "跨页筛选计数覆盖全部匹配行",
                "test_second_page_counts_all_matching_rows",
                20,
            ),
            ("links", "切换筛选从第一页开始", "test_toggle_restarts_at_first_page", 25),
        ],
        "private": [
            (
                "filter_sort",
                "排序及业务筛选组合",
                "test_sort_and_business_filter_do_not_change_counts_across_pages",
                10,
            ),
            (
                "array_date",
                "数组、日期及重复参数的切换",
                "test_selected_array_and_date_toggle_keep_unrelated_repeated_parameters",
                10,
            ),
            (
                "regression",
                "原有无筛选分页不回归",
                "test_unfiltered_pagination_remains_unchanged",
                5,
            ),
        ],
    },
}


def build(contexts, private_tests, output, images):
    if not all(
        re.fullmatch(r"sha256:[a-f0-9]{64}", image) for image in images.values()
    ):
        raise ValueError("Use immutable image IDs from this host")
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    rules = {}
    for task, description in TASKS.items():
        context = contexts / task
        spec = json.loads((context / "manifest.json").read_text(encoding="utf-8"))
        baseline = {
            path: (context / "workspace" / path).read_bytes().decode("utf-8")
            for path in spec["files"]
        }
        criteria, modules = [], {}
        for category in ("public", "private"):
            name = f"{task}-{category}.py"
            source = (
                (context / "workspace/tests/test_public.py").read_bytes()
                if category == "public"
                else (
                    private_tests / ("test_" + task.replace("-", "_") + ".py")
                ).read_bytes()
            )
            (output / name).write_bytes(source)
            modules[name] = hashlib.sha256(source).hexdigest()
            for key, title, method, points in description[category]:
                criteria.append(
                    {
                        "key": key,
                        "name": title,
                        "module": name,
                        "case": description[category + "_class"],
                        "method": method,
                        "max": points,
                        "core": task == "facet-pagination" and category == "public",
                    }
                )
        rules[spec["task_version"]] = {
            "rule_version": task + "-70-30-v2",
            "images": [images[task]],
            "baseline": baseline,
            "writable": spec["writable"],
            "criteria": criteria,
            "modules": modules,
            "manual": manual_rubric(task),
            "review_evidence_required": True,
            "failure_cap": 50 if task == "facet-pagination" else None,
        }
    (output / "manifest.json").write_text(
        json.dumps(
            {"schema_version": 1, "rules": rules}, ensure_ascii=False, sort_keys=True
        ),
        encoding="utf-8",
    )
    from app.grading.datasette import load_rule

    for task, image in images.items():
        version = json.loads((contexts / task / "manifest.json").read_text())[
            "task_version"
        ]
        load_rule(output, version, image)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contexts", required=True, type=Path)
    parser.add_argument("--private-tests", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--column-image", required=True)
    parser.add_argument("--facet-image", required=True)
    args = parser.parse_args()
    build(
        args.contexts,
        args.private_tests,
        args.output,
        {"column-statistics": args.column_image, "facet-pagination": args.facet_image},
    )
    print("可信评测包已生成：两题各客观 70 分、人工 30 分。请仅在评阅服务配置其路径。")


if __name__ == "__main__":
    main()
