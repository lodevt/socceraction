"""Implements the Hybrid-VAEP framework.

Attributes
----------
xfns_default : list(callable)
    The default VAEP features.
xfns_result_default : list(callable)
    The default VAEP features to describe the result of an action

"""
import math
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from sklearn.exceptions import NotFittedError
from sklearn.metrics import brier_score_loss, roc_auc_score

import socceraction.spadl as spadlcfg

from . import features as fs
from . import formula as hybrid_vaep
from . import labels as lab

try:
    import xgboost
except ImportError:
    xgboost = None  # type: ignore
try:
    import catboost
except ImportError:
    catboost = None  # type: ignore
try:
    import lightgbm
except ImportError:
    lightgbm = None  # type: ignore


xfns_default = [
    fs.actiontype_onehot,
    fs.bodypart_onehot,
    fs.time,
    fs.startlocation,
    fs.endlocation,
    fs.startpolar,
    fs.endpolar,
    fs.movement,
    fs.team,
    fs.time_delta,
    fs.space_delta,
    fs.goalscore,
]

xfns_result_default = [
    fs.result_onehot,
    fs.actiontype_result_onehot,
]


class HybridVAEP:
    """
    An implementation of the Hybrid-VAEP framework.

    VAEP (Valuing Actions by Estimating Probabilities) [1]_ defines the
    problem of valuing a soccer player's contributions within a match as
    a binary classification problem and rates actions by estimating its effect
    on the short-term probablities that a team will both score and concede.

    Hybrid-VAEP is an alternative implementation that uses the adjusted vaep
    in :attr:`~socceraction.hybrid-vaep.formula`. This formula does not incorporate
    the result of the previous action, making it possible to credit pass receivers
    and properly value defensive actions.

    Parameters
    ----------
    xfns : list
        List of feature transformers (see :mod:`socceraction.vaep.features`)
        used to describe the game states. Please do not include features describing the result.
        Uses :attr:`~socceraction.hyrbid-vaep.base.xfns_default if None.
    xfns_result : list
        List of feature transformers (see :mod:`socceraction.vaep.features`)
        used to describe result of the game states. Uses :attr:`~socceraction.hybrid-vaep.base.xfns_result_default`
        if None.
    nb_prev_actions : int, default=3  # noqa: DAR103
        Number of previous actions used to decscribe the game state.


    References
    ----------
    .. [1] Tom Decroos, Lotte Bransen, Jan Van Haaren, and Jesse Davis.
        "Actions speak louder than goals: Valuing player actions in soccer." In
        Proceedings of the 25th ACM SIGKDD International Conference on Knowledge
        Discovery & Data Mining, pp. 1851-1861. 2019.
    """

    _spadlcfg = spadlcfg
    _fs = fs
    _lab = lab
    _vaep = hybrid_vaep

    def __init__(
        self,
        xfns: Optional[list[fs.FeatureTransfomer]] = None,
        xfns_result: Optional[list[fs.FeatureTransfomer]] = None,
        nb_prev_actions: int = 3,
    ) -> None:
        self.__models: dict[str, Any] = {"scores": {}, "concedes": {}}
        self.xfns_result = xfns_result_default if xfns_result is None else xfns_result
        self.xfns_standard = (
            xfns_default + self.xfns_result if xfns is None else xfns + self.xfns_result
        )
        self.xfns_resultfree = xfns_default if xfns is None else xfns
        self.yfns = [lab.scores, lab.concedes]
        self.nb_prev_actions = nb_prev_actions

    def compute_features(self, game: pd.Series, game_actions: fs.Actions) -> pd.DataFrame:
        """
        Transform actions to the feature-based representation of game states.

        Parameters
        ----------
        game : pd.Series
            The SPADL representation of a single game.
        game_actions : pd.DataFrame
            The actions performed during `game` in the SPADL representation.

        Returns
        -------
        features : pd.DataFrame
            Returns the feature-based representation of each game state in the game.
        """

        game_actions_with_names = self._spadlcfg.add_names(game_actions)  # type: ignore
        gamestates = self._fs.gamestates(game_actions_with_names, self.nb_prev_actions)
        gamestates = self._fs.play_left_to_right(gamestates, game.home_team_id)
        standard_features = pd.concat([fn(gamestates) for fn in self.xfns_standard], axis=1)

        return standard_features

    def compute_labels(
        self, game: pd.Series, game_actions: fs.Actions  # pylint: disable=W0613
    ) -> pd.DataFrame:
        """
        Compute the labels for each game state in the given game.

        Parameters
        ----------
        game : pd.Series
            The SPADL representation of a single game.
        game_actions : pd.DataFrame
            The actions performed during `game` in the SPADL representation.

        Returns
        -------
        labels : pd.DataFrame
            Returns the labels of each game state in the game.
        """
        game_actions_with_names = self._spadlcfg.add_names(game_actions)  # type: ignore
        return pd.concat([fn(game_actions_with_names) for fn in self.yfns], axis=1)

    def fit(
        self,
        X: pd.DataFrame,
        y: pd.DataFrame,
        learner: str = 'xgboost',
        val_size: float = 0.25,
        tree_params: Optional[dict[str, Any]] = None,
        fit_params: Optional[dict[str, Any]] = None,
    ) -> 'HybridVAEP':
        """
        Fit the model according to the given training data.

        Parameters
        ----------
        X : pd.DataFrame
            Feature representation of the game states
        y : pd.DataFrame
            Scoring and conceding labels for each game state.
        learner : string, default='xgboost'  # noqa: DAR103
            Gradient boosting implementation which should be used to learn the
            model. The supported learners are 'xgboost', 'catboost' and 'lightgbm'.
        val_size : float, default=0.25  # noqa: DAR103
            Percentage of the dataset that will be used as the validation set
            for early stopping. When zero, no validation data will be used.
        tree_params : dict
            Parameters passed to the constructor of the learner.
        fit_params : dict
            Parameters passed to the fit method of the learner.

        Raises
        ------
        ValueError
            If one of the features is missing in the provided dataframe.

        Returns
        -------
        self
            Fitted Hybrid-VAEP model.

        """
        nb_states = len(X)
        idx = np.random.permutation(nb_states)
        # fmt: off
        train_idx = idx[:math.floor(nb_states * (1 - val_size))]
        val_idx = idx[(math.floor(nb_states * (1 - val_size)) + 1):]
        # fmt: on

        # filter feature columns
        cols_standard = self._fs.feature_column_names(self.xfns_standard, self.nb_prev_actions)
        if not set(cols_standard).issubset(set(X.columns)):
            missing_cols = ' and '.join(set(cols_standard).difference(X.columns))
            raise ValueError(f'{missing_cols} are not available in the features dataframe')

        cols_resultfree = self._fs.feature_column_names(self.xfns_resultfree, self.nb_prev_actions)

        # split train and validation data
        X_train_standard, X_train_resultfree, y_train = (
            X.iloc[train_idx][cols_standard],
            X.iloc[train_idx][cols_resultfree],
            y.iloc[train_idx],
        )
        X_val_standard, X_val_resultfree, y_val = (
            X.iloc[val_idx][cols_standard],
            X.iloc[val_idx][cols_resultfree],
            y.iloc[val_idx],
        )

        # train classifiers F(X) = Y
        for col in list(y.columns):
            for model_version in ["standard", "resultfree"]:
                if model_version == "standard":
                    X_val = X_val_standard
                    X_train = X_train_standard
                else:
                    X_val = X_val_resultfree
                    X_train = X_train_resultfree
                eval_set = [(X_val, y_val[col])] if val_size > 0 else None
                if learner == 'xgboost':
                    self.__models[col][model_version] = self._fit_xgboost(
                        X_train, y_train[col], eval_set, tree_params, fit_params
                    )
                elif learner == 'catboost':
                    self.__models[col][model_version] = self._fit_catboost(
                        X_train, y_train[col], eval_set, tree_params, fit_params
                    )
                elif learner == 'lightgbm':
                    self.__models[col][model_version] = self._fit_lightgbm(
                        X_train, y_train[col], eval_set, tree_params, fit_params
                    )
                else:
                    raise ValueError(f'A {learner} learner is not supported')
        return self

    def _fit_xgboost(
        self,
        X: pd.DataFrame,
        y: pd.Series,
        eval_set: Optional[list[tuple[pd.DataFrame, pd.Series]]] = None,
        tree_params: Optional[dict[str, Any]] = None,
        fit_params: Optional[dict[str, Any]] = None,
    ) -> 'xgboost.XGBClassifier':
        if xgboost is None:
            raise ImportError('xgboost is not installed.')
        # Default settings
        if tree_params is None:
            tree_params = dict(n_estimators=100, max_depth=3)
        if fit_params is None:
            fit_params = dict(eval_metric='auc', verbose=True)
        if eval_set is not None:
            val_params = dict(early_stopping_rounds=10, eval_set=eval_set)
            fit_params = {**fit_params, **val_params}
        # Train the model
        model = xgboost.XGBClassifier(**tree_params)
        return model.fit(X, y, **fit_params)

    def _fit_catboost(
        self,
        X: pd.DataFrame,
        y: pd.Series,
        eval_set: Optional[list[tuple[pd.DataFrame, pd.Series]]] = None,
        tree_params: Optional[dict[str, Any]] = None,
        fit_params: Optional[dict[str, Any]] = None,
    ) -> 'catboost.CatBoostClassifier':
        if catboost is None:
            raise ImportError('catboost is not installed.')
        # Default settings
        if tree_params is None:
            tree_params = dict(eval_metric='BrierScore', loss_function='Logloss', iterations=100)
        if fit_params is None:
            is_cat_feature = [c.dtype.name == 'category' for (_, c) in X.iteritems()]
            fit_params = dict(
                cat_features=np.nonzero(is_cat_feature)[0].tolist(),
                verbose=True,
            )
        if eval_set is not None:
            val_params = dict(early_stopping_rounds=10, eval_set=eval_set)
            fit_params = {**fit_params, **val_params}
        # Train the model
        model = catboost.CatBoostClassifier(**tree_params)
        return model.fit(X, y, **fit_params)

    def _fit_lightgbm(
        self,
        X: pd.DataFrame,
        y: pd.Series,
        eval_set: Optional[list[tuple[pd.DataFrame, pd.Series]]] = None,
        tree_params: Optional[dict[str, Any]] = None,
        fit_params: Optional[dict[str, Any]] = None,
    ) -> 'lightgbm.LGBMClassifier':
        if lightgbm is None:
            raise ImportError('lightgbm is not installed.')
        if tree_params is None:
            tree_params = dict(n_estimators=100, max_depth=3)
        if fit_params is None:
            fit_params = dict(eval_metric='auc', verbose=True)
        if eval_set is not None:
            val_params = dict(early_stopping_rounds=10, eval_set=eval_set)
            fit_params = {**fit_params, **val_params}
        # Train the model
        model = lightgbm.LGBMClassifier(**tree_params)
        return model.fit(X, y, **fit_params)

    def _estimate_probabilities(self, X: pd.DataFrame) -> pd.DataFrame:
        # filter feature columns
        cols_standard = self._fs.feature_column_names(self.xfns_standard, self.nb_prev_actions)
        if not set(cols_standard).issubset(set(X.columns)):
            missing_cols = ' and '.join(set(cols_standard).difference(X.columns))
            raise ValueError(f'{missing_cols} are not available in the features dataframe')
        cols_resultfree = self._fs.feature_column_names(self.xfns_resultfree, self.nb_prev_actions)

        Y_hat = pd.DataFrame()
        for col in self.__models:
            for model_version in ["standard", "resultfree"]:
                if model_version == "standard":
                    cols = cols_standard
                else:
                    cols = cols_resultfree
                Y_hat[col + "-" + model_version] = [
                    p[1] for p in self.__models[col][model_version].predict_proba(X[cols])
                ]

        return Y_hat

    def rate(
        self, game: pd.Series, game_actions: fs.Actions, game_states: Optional[fs.Features] = None
    ) -> pd.DataFrame:
        """
        Compute the VAEP rating for the given game states.

        Parameters
        ----------
        game : pd.Series
            The SPADL representation of a single game.
        game_actions : pd.DataFrame
            The actions performed during `game` in the SPADL representation.
        game_states : pd.DataFrame, default=None
            DataFrame with the game state representation of each action. If
            `None`, these will be computed on-th-fly.

        Raises
        ------
        NotFittedError
            If the model is not fitted yet.

        Returns
        -------
        ratings : pd.DataFrame
            Returns the VAEP rating for each given action, as well as the
            offensive and defensive value of each action.
        """
        if not self.__models:
            raise NotFittedError()

        game_actions_with_names = self._spadlcfg.add_names(game_actions)  # type: ignore
        if game_states is None:
            game_states = self.compute_features(game, game_actions)

        y_hat = self._estimate_probabilities(game_states)
        p_scores_standard, p_scores_resultfree, p_concedes_standard, p_concedes_resultfree = (
            y_hat["scores-standard"],
            y_hat["scores-resultfree"],
            y_hat["concedes-standard"],
            y_hat["concedes-resultfree"],
        )
        vaep_values = self._vaep.value(
            game_actions_with_names,
            p_scores_standard,
            p_scores_resultfree,
            p_concedes_standard,
            p_concedes_resultfree,
        )
        return vaep_values

    def score(self, X: pd.DataFrame, y: pd.DataFrame) -> dict[str, dict[str, float]]:
        """Evaluate the fit of the model on the given test data and labels.

        Parameters
        ----------
        X : pd.DataFrame
            Feature representation of the game states.
        y : pd.DataFrame
            Scoring and conceding labels for each game state.

        Raises
        ------
        NotFittedError
            If the model is not fitted yet.

        Returns
        -------
        score : dict
            The Brier and AUROC scores for both binary classification problems.
        """
        if not self.__models:
            raise NotFittedError()

        y_hat = self._estimate_probabilities(X)

        scores: dict[str, dict[str, float]] = {}
        for col in self.__models:
            scores[col] = {}
            scores[col]['brier'] = brier_score_loss(y[col], y_hat[col])
            scores[col]['auroc'] = roc_auc_score(y[col], y_hat[col])

        return scores