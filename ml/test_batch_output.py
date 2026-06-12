"""Tests for SageMaker batch .out parsing."""

from ml.batch_output import parse_batch_transform_body, parse_jsonl_body


def test_parse_json_array():
    body = '[{"region_grid":0,"predicted_order_count":2.6},{"region_grid":1,"predicted_order_count":3.0}]'
    rows = parse_batch_transform_body(body)
    assert len(rows) == 2
    assert rows[0]["region_grid"] == 0


def test_parse_csv():
    body = "region_grid,hour,predicted_order_count\n0,7,2.6\n1,7,3.0\n"
    rows = parse_batch_transform_body(body)
    assert len(rows) == 2


def test_parse_jsonl():
    body = '{"a": 1}\n{"a": 2}\n'
    assert len(parse_jsonl_body(body)) == 2
