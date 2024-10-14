import logging
from numbers import Number
from pathlib import Path
from typing import Callable, Optional, Tuple, Union

import numpy as np
import pandas as pd
import sklearn.metrics as metrics

from .confidence import PRConfidenceParam, ROCConfidenceParam, confidence_dict
from .decorators import export

DEFAULT_RHO = 1 / 3

PathLike = Union[str, Path]
COUNTS = ["TP", "FP", "TN", "FN"]
THRESHOLD = "Threshold"
STATNAMES = COUNTS + [
    "Accuracy",
    "Sensitivity",
    "Specificity",
    "PPV",
    "NPV",
    "Flagged",
    "LR+",
    "NetBenefitScore",
]


@export
class MetricGenerator:
    def __init__(self, metric_names: list[str], metric_fn: Callable[..., dict[str, float]]):
        """
        A class that generates metrics from a dataframe.
        Keeps track of available metric names as well as the function to call to generate them.
        Delegates the call to the metric fuction, and returns the results as a dictionary.
        Cannot handle parametrized metrics (one value per threhsold)

        Parameters
        ----------
        metric_names : list[str]
            List of metric names that can be generated by the metric function.
            The metric "Count" is reserved.
        metric_fn : Callable[[pd.DataFrame, list[str], ...], list[str], dict[str, float]]
            Function that generates metrics from a dataframe.
        """
        if not metric_names:
            raise ValueError("metric_names must be a non-empty list of supported metrics")
        if not hasattr(metric_fn, "__call__"):
            raise ValueError("metric_fn must be a callable function")
        if "Count" in metric_names:
            raise ValueError("Count is a reserved metric name and cannot be used.")
        self.metric_names = metric_names
        self.metric_fn = metric_fn

    def __call__(self, dataframe: pd.DataFrame, metric_names: list[str] = None, **kwargs) -> dict[str, float]:
        """
        Generate metrics from a dataframe.

        Parameters
        ----------
        dataframe : pd.DataFrame
            The dataframe to generate metrics from.

        Returns
        -------
        dict[str, float]
            A dictionary of metric names and their values.
        """
        if metric_names is None:
            metric_names = self.metric_names
        if not set(metric_names).issubset(self.metric_names):
            raise ValueError(f"Invalid metric names: {set(metric_names) - set(self.metric_names)}")
        return self.delegate_call(dataframe, metric_names, **kwargs)

    def delegate_call(self, dataframe: pd.DataFrame, metric_names: list[str], **kwargs) -> dict[str, float]:
        """
        Generate metrics from a dataframe.

        Parameters
        ----------
        dataframe : pd.DataFrame
            The dataframe to generate metrics from.

        Returns
        -------
        dict[str, float]
            A dictionary of metric names and their values.
        """
        return self.metric_fn(dataframe, metric_names, **kwargs)

    def __repr__(self):
        return f"MetricGenerator(metric_names={self.metric_names}, metric_fn={self.metric_fn.__name__})"


@export
class BinaryClassifierMetricGenerator(MetricGenerator):
    def __init__(self, rho: float = None):
        """
        A class that generates Binary classifier metrics from a dataframe.
        Keeps track of available metric names as well as the function to call to generate them.
        Delegates the call to the metric fuction, and returns the results as a dictionary.
        Cannot handle parametrized metrics (one value per threhsold)

        rho : float, optional
            The relative risk reduction for NNT calculation, by default DEFAULT_RHO.
        """
        self.rho = rho or DEFAULT_RHO

    @property
    def metric_names(self):
        return STATNAMES + [f"NNT@{self.rho:0.3n}"]

    def delegate_call(
        self, dataframe: pd.DataFrame, metric_names: list[str], *, target_col, score_col, score_threshold: float = 0.5
    ) -> dict[str, float]:
        """
        Generate metrics from a dataframe.

        Parameters
        ----------
        dataframe : pd.DataFrame
            The dataframe to generate metrics from.
        metric_names : list[str]
            List of metric names to generate.
        target_col : str
            The column in the dataframe that contains the true labels.
        score_col : str
            The column in the dataframe that contains the predicted scores.
        score_threshold : float, optional
            The threshold to use for binary classification, by default 0.5.

        Returns
        -------
        dict[str, float]
            A dictionary of metric names and their values.
        """
        res = calculate_binary_stats(dataframe, target_col, score_col, score_threshold, rho=self.rho)
        res = {k: v for k, v in res.items() if k in metric_names}
        return res

    def __repr__(self):
        return f"BinaryClassifierMetricGenerator(rho={self.rho})"


@export
def assert_valid_performance_metrics_df(df: pd.DataFrame, needs_columns: list = None) -> bool:
    """
    Determines whether a passed dataframe has either all or a subset of columns that likely indicate
    it was generated by calculate_bin_stats.

    Parameters
    ----------
    df : pd.DataFrame
        Dataframe under investigation.
    needs_columns : list
        List of columns that need to be present in order to be determined valid.
        Default is all columns normally generated by calculate_bin_stats.

    Returns
    -------
    bool
        Whether it is likely a valid performance metrics df.
    """
    performance_columns = needs_columns or [
        "Threshold",
        "Accuracy",
        "Sensitivity",
        "Specificity",
        "PPV",
        "NPV",
    ]
    if (df is None) or (not all([item in df.columns for item in performance_columns])):
        raise ValueError(
            "Passed performance frame does not have required columns: "
            + f"{performance_columns}.\nMissing {set(performance_columns) - set(df.columns)}"
        )


@export
def calculate_binary_stats(
    dataframe: pd.DataFrame,
    target_col: str,
    score_col: str,
    score_threshold: float = 0.5,
    rho: float = None,
) -> dict[str, float]:
    """
    Generates binary classifier metrics from a dataframe, as a specific threshold

    Parameters
    ----------
    dataframe : pd.DataFrame
        The dataframe to generate metrics from.
    target_col : str
        The column in the dataframe that contains the true labels.
    score_col : str
        The column in the dataframe that contains the predicted scores.
    score_threshold : float, optional
        The threshold to use for binary classification, by default 0.5.
    rho : float, optional
        The relative risk reduction for NNT calculation, by default DEFAULT_RHO.
    """
    rho = rho or DEFAULT_RHO
    score_threshold_integer = int(score_threshold * 100)
    y_true = dataframe[target_col]
    y_pred = dataframe[score_col]
    stats = calculate_bin_stats(y_true, y_pred, rho=rho)
    return stats.iloc[100 - score_threshold_integer].to_dict()


@export
def calculate_bin_stats(
    y_true: Optional[pd.Series] = None,
    y_pred: Optional[pd.Series] = None,
    keep_score_values: bool = False,
    not_point_thresholds: bool = False,
    rho: float = None,
) -> pd.DataFrame:
    """
    Calculate summary statistics from y_true and y_pred (y_proba[:,1] for binary classification) arrays.
    Supports y_true & y_pred as individaul series-likes or as a dataframe with true and proba columns.

    Parameters
    ----------
    y_true : Optional[pd.Series], optional
        Series like of binary labels.
    y_pred : Optional[pd.Series], optional
        Series like of probabilities for positive class.
    keep_score_values : bool, optional
        Flag to prevent attempts to convert score to percentage (0-100), default False.
    not_point_thresholds : bool, optional
        If True, does not use point thresholds, by default False; uses 0-100.
    rho : float, optional
        The relative risk reduction for NNT calculation, by default DEFAULT_RHO.

    Returns
    -------
    pd.DataFrame of stats, rows for each threshold value between 0 and 100 with columns for basic statistics.
    """
    rho = rho or DEFAULT_RHO
    y_true = y_true.astype(float)  # Expect numeric labels

    keep = ~(np.isnan(y_true) | np.isnan(y_pred))
    if not keep.any():
        return pd.DataFrame(columns=[THRESHOLD] + STATNAMES)

    # reduce
    y_true = y_true[keep]
    y_pred = y_pred[keep].round(5)

    n = len(y_true)

    if not keep_score_values:
        y_pred = as_percentages(y_pred)

    fps, tps, thresholds = _bin_class_curve(y_true, y_pred)

    # Add extrema if needed (logits); tree-likes could make predictions of 1 and 0
    if np.min(y_pred) > 0:
        thresholds = np.hstack((thresholds, [0]))
        tps = np.hstack((tps, tps[-1]))
        fps = np.hstack((fps, fps[-1]))
    if (not keep_score_values) and (np.max(y_pred) < 100):
        thresholds = np.hstack(([100], thresholds))
        tps = np.hstack(([0], tps))
        fps = np.hstack(([0], fps))

    # Reduce thresholds to table 0-100
    # This can reintroduce redundant thresholds, particularly in sparse regions
    if not not_point_thresholds:
        threshold_ix, thresholds = _point_thresholds(thresholds)
        tps = tps[threshold_ix]
        fps = fps[threshold_ix]

    with np.errstate(invalid="ignore", divide="ignore"):
        # fps[-1] = N,  tps[-1] = T
        fpr = fps / fps[-1]
        tpr = tps / tps[-1]

        ppv = tps / (tps + fps)
        ppv[np.isnan(ppv)] = 0

        # TN / TN + FN
        npv = np.divide(fps[-1] - fps, (fps[-1] - fps) + (tps[-1] - tps))
        npv[np.isnan(npv)] = 1

        lr = tpr / fpr

        # re-implementation of metrics from med_metrics package, see _calculate_nnt for full citation
        nnt = calculate_nnt(ppv)
        nbs = (tps - fps * (thresholds / (100 - thresholds))) / n

    accuracy = (tps + (fps[-1] - fps)) / n
    ppcr = (tps + fps) / n

    # NOTE: Don't set index to be threshold because it's a float and this
    # makes lookup annoying due to tolerance settings
    stats = pd.DataFrame(
        np.column_stack(
            (
                thresholds,
                tps,
                fps,
                fps[-1] - fps,
                tps[-1] - tps,
                accuracy,
                tpr,
                1 - fpr,
                ppv,
                npv,
                ppcr,
                lr,
                nbs,
                nnt,
            )
        ),
        columns=[THRESHOLD] + STATNAMES + [f"NNT@{rho:0.3n}"],
    )

    stats[COUNTS] = stats[COUNTS].fillna(0).astype(int)  # Strengthen dtypes on counts
    return stats


@export
def calculate_nnt(arr: np.ndarray, rho: Optional[Number | None] = None) -> np.ndarray:
    """
    Calculates NNT (Number Needed to Treat) for the relative risk reduction, rho, and a
    perfect-ARR (absolute risk reduction), ie PPV.

    This formulation and the related ARR and Net Benefit calculation is originally
    from eotles/med_metrics [#med_metrics]_.

    Parameters
    ----------
    arr : np.ndarray
        The array of absolute risk reductions for each threshold assuming a rho of 1
    rho : Number, optional
        The estimated relative risk reduction, by default is DEFAULT_RHO (1/3).

    Returns
    -------
    np.ndarray
        the NNT for each threshold.

    References
    ----------
    .. [#med_metrics] eotles/med_metrics: Initial public release. Zenodo; 2024.
           http://dx.doi.org/10.5281/ZENODO.10514448
    """
    rho = rho or DEFAULT_RHO

    # Divide by zero is ok
    with np.errstate(invalid="ignore", divide="ignore"):
        nnt = 1 / (rho * arr)

    return nnt


@export
def calculate_eval_ci(stats: pd.DataFrame, truth: pd.Series, output: pd.Series, conf: Number = 0.95) -> dict:
    """
    Calculate confidence intervals for ROC, PR, and other performance metrics from a stats frame.

    Parameters
    ----------
    stats : pd.DataFrame
        The performance statistics, generated by calculate_bin_stats or similar.
    truth : pd.Series
        The series of data with the ground truth labeling that is associated with the stats frame.
    output : pd.Series
        The series of model output associated with the stats frame.
    conf : Number, optional
        The confidence level for calculation, by default 0.95.

    Returns
    -------
    dict
        A dictionary of confidence details for an evaluation plot.
    """
    required_columns = ["TP", "FP", "TN", "FN", "Threshold", "Sensitivity", "PPV"]
    assert_valid_performance_metrics_df(stats, required_columns)

    conf = confidence_dict(conf)
    if truth is None or output is None:
        return conf, None, None, None

    roc_conf = ROCConfidenceParam(conf)
    aucpr_conf = PRConfidenceParam(conf)
    with np.errstate(invalid="ignore", divide="ignore"):
        # roc
        thresholds, tpr, fpr, auc_region = roc_conf.region(roc_conf, truth, output)
        auc_interval = roc_conf.interval(roc_conf, truth, output)
        # pr
        aucpr_interval = aucpr_conf.interval(
            aucpr_conf,
            metrics.auc(stats.Sensitivity, stats.PPV),
            stats[["TP", "FP", "TN", "FN"]].iloc[0].sum(),
        )

    ci_data = {
        "roc": {
            "Threshold": thresholds,
            "TPR": tpr,
            "FPR": fpr,
            "region": auc_region,
            "interval": auc_interval,
        },
        "pr": {"interval": aucpr_interval},
        "conf": conf,
    }
    return ci_data


def _bin_class_curve(
    y_true: Union[np.ndarray, pd.Series], y_pred: Union[np.ndarray, pd.Series]
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Inspired by binary_clf_curve.
    Does core calculations for determining performance stats.
    """
    # Sort both arrays to have predictions descending
    sort_ix = np.argsort(y_pred, kind="mergesort")[::-1]
    y_true = np.array(y_true)[sort_ix]
    y_pred = np.array(y_pred)[sort_ix]

    # Find where the threshold changes
    distinct_ix = np.where(np.diff(y_pred))[0]
    threshold_idxs = np.r_[distinct_ix, y_true.size - 1]

    # Add up the true positives and infer false ones
    tps = np.cumsum(y_true)[threshold_idxs]
    fps = 1 + threshold_idxs - tps

    return fps, tps, y_pred[threshold_idxs]


def _point_thresholds(orig_thresholds: np.ndarray) -> np.ndarray:
    """
    Convert thresholds to percent increments (0.01) between 0 to 1.
    """
    if orig_thresholds.max() < 1:
        logging.warning("Passed thresholds do not extend to maximum of 1.")
    thresholds = np.arange(0, 101)[::-1]
    ixs = np.digitize(thresholds, orig_thresholds, right=True) - 1
    ixs = np.where(ixs < 0, 0, ixs)  # Clip to 0
    ixs[-1] = -1  # Keep the last threshold
    return ixs, thresholds


def as_percentages(proba: np.ndarray) -> np.ndarray:
    """
    Converts a probability in the 0-1 range to a percentage in the 0-100 range.

    Parameters
    ----------
    proba : np.ndarray
        array-like list of probabilities.

    Returns
    -------
    np.ndarray
        array-like list of percentages.
    """
    if proba.max() < 2:
        proba *= 100
    return proba


def as_probabilities(perc: np.ndarray) -> np.ndarray:
    """
    Converts a percentage in the 0-100 range to a probability in the 0-1 range.

    Parameters
    ----------
    perc : np.ndarray
        array-like list of percentages.

    Returns
    -------
    np.ndarray
        array-like list of probabilities.
    """
    if perc.max() > 2:
        perc /= 100
    return perc
