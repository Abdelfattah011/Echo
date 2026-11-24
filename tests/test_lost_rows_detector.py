
from Engine.detectors.lost_rows_detector import LostRowsDetector


def make_steps(join_type="LEFT", where_aliases=None, null_rejecting=True):
    steps = [
        {
            "step_id": "step_2",
            "step_type": "JOIN",
            "scope": "main",
            "statement_index": 0,
            "status": "success",
            "join_details": {
                "join_type": join_type,
                "target_table": "orders",
                "target_alias": "o",
            },
        }
    ]

    if where_aliases is not None:
        steps.append({
            "step_id": "step_3",
            "step_type": "WHERE",
            "scope": "main",
            "statement_index": 0,
            "status": "success",
            "where_details": {
                "conjuncts": [
                    {
                        "sql": "o.status = 'Completed'",
                        "aliases": where_aliases,
                        "null_rejecting": null_rejecting,
                    }
                ]
            },
        })

    return steps


def test_detects_left_join_with_null_rejecting_filter():
    detector = LostRowsDetector()

    report = detector.detect(
        make_steps(where_aliases=["o"]),
        [],
    )

    assert report is not None
    assert report["bug_type"] == "Lost Rows in a LEFT JOIN"
    assert report["suspicious_step_id"] == "step_2"


def test_ignores_left_join_without_where():
    detector = LostRowsDetector()

    report = detector.detect(
        make_steps(),
        [],
    )

    assert report is None


def test_ignores_filter_on_left_table():
    detector = LostRowsDetector()

    report = detector.detect(
        make_steps(where_aliases=["c"]),
        [],
    )

    assert report is None


def test_ignores_non_null_rejecting_filter():
    detector = LostRowsDetector()

    report = detector.detect(
        make_steps(where_aliases=["o"], null_rejecting=False),
        [],
    )

    assert report is None


def test_ignores_inner_join():
    detector = LostRowsDetector()

    report = detector.detect(
        make_steps(join_type="INNER", where_aliases=["o"]),
        [],
    )

    assert report is None
