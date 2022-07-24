# %%
# Standard lib
from pathlib import Path
from dataclasses import dataclass, field
import pickle
# Third party
import numpy as np
import pandas as pd
import optuna.integration.lightgbm as opt_lgb
import lightgbm as lgb
from sklearn import model_selection
from sklearn.model_selection import KFold
from sklearn.preprocessing import StandardScaler
from sklearn.decomposition import PCA
import plotly.express as px
from tqdm import tqdm
from matplotlib import pyplot as plt

USE_PICKLE = False
PARAM_SEARCH = True


@dataclass
class Config():
    train_data_path: Path = Path(r"Data\feather_data\train_data.ftr")
    test_data_path: Path = Path(r"Data\feather_data\test_data.ftr")
    train_pca_data_path: Path = Path(r"Data\feather_data\train_pca_data.ftr")
    test_pca_data_path: Path = Path(r"Data\feather_data\test_pca_data.ftr")
    train_label_path: Path = Path(
        r"Data\amex-default-prediction\train_labels.csv"
    )
    sample_submission_path: Path = Path(
        r"Data\amex-default-prediction\sample_submission.csv"
    )
    result_submission_path: Path = Path(r"Data\result_submission.csv")
    model_param: dict = field(default_factory=lambda: {
        "device": "gpu",
        "objective": "binary",
        "boosting": "dart",
        "max_bin": 255,
        "max_depth": -1,
        "learning_rate": 0.05
    })
    category_param: list = field(
        default_factory=lambda: [
            'D_63', 'D_64',  # categorical
            'B_30', 'B_38',
            'D_114', 'D_116', 'D_117', 'D_120', 'D_126', 'D_66', 'D_68'
        ]
    )
    remove_param: list = field(
        default_factory=lambda: [
            "S_2",
            "D_73", "D_87", "D_88", "D_108", "D_110", "D_111", "B_39", "B_42"
        ]
    )


CFG = Config()


def amex_metric(y_true: np.array, y_pred: np.array) -> float:
    """
    評価関数、ジニ係数
    """

    # count of positives and negatives
    n_pos = y_true.sum()
    n_neg = y_true.shape[0] - n_pos

    # sorting by descring prediction values
    indices = np.argsort(y_pred)[::-1]
    # preds = y_pred[indices]
    target = y_true[indices]

    # filter the top 4% by cumulative row weights
    weight = 20.0 - target * 19.0
    cum_norm_weight = (weight / weight.sum()).cumsum()
    four_pct_filter = cum_norm_weight <= 0.04

    # default rate captured at 4%
    d = target[four_pct_filter].sum() / n_pos

    # weighted gini coefficient
    lorentz = (target / n_pos).cumsum()
    gini = ((lorentz - cum_norm_weight) * weight).sum()

    # max weighted gini coefficient
    gini_max = 10 * n_neg * (1 - 19 / (n_pos + 20 * n_neg))

    # normalized weighted gini coefficient
    g = gini / gini_max

    return 0.5 * (g + d)


def lgb_amex_metric(y_pred, y_true):
    """
    lgb wrapper
    """
    return (
        "Score",
        amex_metric(y_true.get_label(), y_pred),
        True
    )

# %%


def preprocess(data: pd.DataFrame):

    data.sort_values(["customer_ID", "S_2"], inplace=True)
    data.fillna(0, inplace=True)

    if len(set(CFG.remove_param) & set(data.columns)) != 0:
        data = data.drop(CFG.remove_param, axis=1)

    data = pd.get_dummies(data, columns=CFG.category_param)

    return (
        data
        .set_index("customer_ID", drop=True)
    )


train_data = preprocess(pd.read_feather(CFG.train_data_path))
train_labels = (
    pd.read_csv(CFG.train_label_path)
    .set_index('customer_ID', drop=True)
    .sort_index()
)
# %%


def transpose_data(data: pd.DataFrame):

    res_list = []

    for cid, df in data.groupby("customer_ID").__iter__():
        df_val = list(df.values)
        if (df_len := len(df_val)) != 13:
            df_val = [0] * (13 - df_len) + df_val
        res_list.append([cid] + df_val)

    return pd.DataFrame(
        data=res_list,
        columns=np.concatenate(
            [["id"], [f"series_{i}" for i in np.arange(13)]]
        )
    ).set_index("id", drop=True)


def create_model(data: pd.DataFrame):

    ss = StandardScaler()
    pca = PCA(n_components=1, svd_solver="full")

    pca.fit(ss.fit_transform(data))

    return pca, np.cumsum(pca.explained_variance_ratio_)


match USE_PICKLE:
    case True:
        with open("model_score.pickle", mode="rb") as f:
            model_dict = pickle.load(f)
            score_dict = pickle.load(f)
    case False:
        model_dict = {}
        score_dict = {}
        for col in tqdm(train_data.columns):
            t_data = transpose_data(train_data[col])
            model_dict[col], score_dict[col] = create_model(t_data)

        with open("model_score.pickle", mode="wb") as f:
            pickle.dump(model_dict, f)
            pickle.dump(score_dict, f)

score_df = pd.DataFrame(score_dict.values(), index=score_dict.keys())
fig = px.bar(score_df)
fig.show()
# %%
comp_dict = {}
for col in tqdm(train_data.columns):
    ss = StandardScaler()
    res = model_dict[col].transform(
        ss.fit_transform(transpose_data(train_data[col]))
    )
    comp_dict[f"PA_{col}"] = res.reshape(-1)

comp_df = pd.DataFrame(
    data=comp_dict,
    index=train_labels.index
)

x_train, x_valid, y_train, y_valid = model_selection.train_test_split(
    comp_df,
    train_labels["target"],
    test_size=0.2,
    random_state=0
)
train_set = lgb.Dataset(x_train, y_train)
valid_set = lgb.Dataset(x_valid, y_valid, reference=train_set)

match PARAM_SEARCH:
    case True:
        tuner = opt_lgb.LightGBMTunerCV(
            CFG.model_param,
            train_set=train_set,
            feval=lgb_amex_metric,
            num_boost_round=500,
            folds=KFold(n_splits=3),
            callbacks=[
                lgb.early_stopping(50),
                lgb.log_evaluation(0),
            ]
        )
        tuner.run()
        param = tuner.best_params
    case False:
        param = {
            'feature_pre_filter': False,
            'lambda_l1': 1.4226819053888403e-06,
            'lambda_l2': 1.9956933606815553e-07,
            'num_leaves': 256,
            'feature_fraction': 0.4,
            'bagging_fraction': 0.44442822147008115,
            'bagging_freq': 5,
            'min_child_samples': 50,
            'num_iterations': 1000
            # 0.7420070828137559
        }

model = lgb.train(
    CFG.model_param | param,
    train_set=train_set,
    valid_sets=valid_set,
    feval=lgb_amex_metric,
    num_boost_round=1000,
    callbacks=[
        lgb.early_stopping(100),
        lgb.log_evaluation(0),
    ]
)
# %%
result = model.predict(x_valid)
score = amex_metric(y_valid, result)
print(model.params, score, sep="\n")
fig = px.histogram(result, nbins=100)
fig.show()
lgb.plot_importance(model)
plt.show()
# %%
