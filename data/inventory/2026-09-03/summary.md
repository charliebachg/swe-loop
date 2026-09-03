# pandas 3 migration inventory, unit tests

| run | tests | failed/error |
|---|---|---|
| r1 pandas 2.3.3 default (baseline) | 13892 | 2 |
| r2 pandas 2.3.3 `-W error::FutureWarning` | 40 | 39 |
| r3 pandas 3.0.5 default | 0 | 0 |

Rows after removing baseline failures: **39** (forward-looking 39, actual breakage 0; pandas-related 39, other 0)

## r2-forward: by class

| class | rows | distinct files | distinct tests |
|---|---|---|---|
| inplace | 22 | 2 | 22 |
| pandas-other | 7 | 4 | 7 |
| downcasting | 4 | 1 | 4 |
| concat-empty | 3 | 1 | 3 |
| datetime-unit | 1 | 1 | 1 |
| fillna-method | 1 | 1 | 1 |
| chained-assignment | 1 | 1 | 1 |

### r2-forward: top source files

- `superset/charts/client_processing.py` (26)
- `tests/unit_tests/utils/excel_tests.py` (4)
- `superset/utils/pandas_postprocessing/pivot.py` (3)
- `superset/models/helpers.py` (1)
- `tests/unit_tests/pandas_postprocessing/test_rank.py` (1)
- `superset/utils/pandas_postprocessing/resample.py` (1)
- `superset/utils/pandas_postprocessing/rolling.py` (1)
- `superset/utils/pandas_postprocessing/utils.py` (1)
- `tests/unit_tests/pandas_postprocessing/test_sort.py` (1)

### r2-forward: top test files

- `tests.unit_tests.charts.test_client_processing` (26)
- `tests.unit_tests.utils.excel_tests` (4)
- `tests.unit_tests.pandas_postprocessing.test_pivot` (3)
- `tests.unit_tests.pandas_postprocessing.test_rolling` (2)
- `tests.unit_tests.common.test_time_shifts` (1)
- `tests.unit_tests.pandas_postprocessing.test_rank` (1)
- `tests.unit_tests.pandas_postprocessing.test_resample` (1)
- `tests.unit_tests.pandas_postprocessing.test_sort` (1)

### r2-forward: sample messages

- [datetime-unit] `superset/models/helpers.py:345` FutureWarning: In a future version of pandas, parsing datetimes with mixed time zones will raise an error unless `utc=True`. Please specify `utc=True` to opt in
- [inplace] `superset/charts/client_processing.py:639` FutureWarning: A value is trying to be set on a copy of a DataFrame or Series through chained assignment using an inplace method.
The behavior will change in pa
- [downcasting] `superset/charts/client_processing.py:754` FutureWarning: Downcasting behavior in `replace` is deprecated and will be removed in a future version. To retain the old behavior, explicitly call `result.infe
- [pandas-other] `superset/charts/client_processing.py:557` FutureWarning: The previous implementation of stack is deprecated and will be removed in a future version of pandas. See the What's New notes for pandas 2.1.0 f
- [pandas-other] `tests/unit_tests/pandas_postprocessing/test_rank.py:35` FutureWarning: DataFrameGroupBy.apply operated on the grouping columns. This behavior is deprecated, and in a future version of pandas the grouping columns will
- [fillna-method] `superset/utils/pandas_postprocessing/resample.py:85` FutureWarning: DataFrame.interpolate with object dtype is deprecated and will raise in a future version. Call obj.infer_objects(copy=False) before interpolating
- [pandas-other] `superset/utils/pandas_postprocessing/rolling.py:102` FutureWarning: the 'quantile' keyword is deprecated, use 'q' instead.
- [inplace] `superset/utils/pandas_postprocessing/utils.py:231` FutureWarning: Setting an item of incompatible dtype is deprecated and will raise in a future error of pandas. Value '[nan 12.]' has dtype incompatible with int
- [chained-assignment] `tests/unit_tests/pandas_postprocessing/test_sort.py:37` FutureWarning: Series.__getitem__ treating keys as positions is deprecated. In a future version, integer keys will always be treated as labels (consistent with 
- [pandas-other] `tests/unit_tests/utils/excel_tests.py:41` FutureWarning: Passing bytes to 'read_excel' is deprecated and will be removed in a future version. To read from a byte string, wrap it in a `BytesIO` object.

## r3-break: by class

| class | rows | distinct files | distinct tests |
|---|---|---|---|

### r3-break: top source files


### r3-break: top test files


### r3-break: sample messages

