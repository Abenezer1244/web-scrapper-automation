import pytest
from pathlib import Path
from src.utils import DataExporter

SAMPLE_DATA = [
    {'name': 'Alice', 'age': 30, 'city': 'Seattle'},
    {'name': 'Bob', 'age': 25, 'city': 'Portland'},
]


@pytest.fixture
def exporter(tmp_path):
    return DataExporter(export_dir=str(tmp_path))


def test_export_csv(exporter):
    path = exporter.to_csv(SAMPLE_DATA, filename='test')
    assert path is not None
    assert path.exists()
    assert path.suffix == '.csv'


def test_export_json(exporter):
    path = exporter.to_json(SAMPLE_DATA, filename='test')
    assert path is not None
    assert path.exists()
    assert path.suffix == '.json'


def test_export_excel(exporter):
    path = exporter.to_excel(SAMPLE_DATA, filename='test')
    assert path is not None
    assert path.exists()
    assert path.suffix == '.xlsx'


def test_export_empty_data_returns_none(exporter):
    result = exporter.to_csv([], filename='empty')
    assert result is None


def test_export_dispatch_csv(exporter):
    path = exporter.export(SAMPLE_DATA, filename='test', format='csv')
    assert path.suffix == '.csv'


def test_export_dispatch_invalid_format(exporter):
    with pytest.raises(ValueError):
        exporter.export(SAMPLE_DATA, format='xml')
