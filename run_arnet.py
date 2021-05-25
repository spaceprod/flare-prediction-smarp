import time
import argparse
import cProfile, pstats
from pathlib import Path
import mlflow
import pytorch_lightning as pl

from arnet import utils
from arnet.dataset import ActiveRegionDataModule
from arnet.modeling.learner import Learner, build_test_logger
from arnet.config import cfg


def train(cfg, dm, resume=False):
    callbacks = [
        pl.callbacks.early_stopping.EarlyStopping(
            monitor='validation/auc',
            patience=cfg.LEARNER.PATIENCE,
            mode='max',
            #verbose=True,
        ),
        pl.callbacks.ModelCheckpoint(
            monitor='validation/auc',
            save_top_k=1,
            mode='max',
        ),
    ]
    kwargs = cfg.TRAINER.todict()
    kwargs.setdefault('callbacks', []).extend(callbacks)
    trainer = pl.Trainer(**kwargs)
    if resume:
        learner = Learner.load_from_checkpoint(resume, cfg=cfg)
    else:
        learner = Learner(cfg)
    trainer.fit(learner, datamodule=dm)
    return trainer.checkpoint_callback.best_model_path


def test(cfg, dm):
    learner = Learner.load_from_checkpoint(cfg.LEARNER.CHECKPOINT, cfg=cfg)
    logger = build_test_logger(learner, testmode='test')
    trainer = pl.Trainer(logger=logger, **cfg.TRAINER.todict())
    trainer.test(learner, datamodule=dm)


def visualize(cfg, dm):
    learner = Learner.load_from_checkpoint(cfg.LEARNER.CHECKPOINT, cfg=cfg)
    logger = build_test_logger(learner, testmode='visualize')
    trainer = pl.Trainer(logger=logger, **cfg.TRAINER.todict())

    learner.testmode = dm.testmode = 'visualize_predictions'
    trainer.test(learner, datamodule=dm)

    learner.testmode = dm.testmode = 'visualize_features'
    trainer.test(learner, datamodule=dm)


def launch(config, modes, resume, opts):
    """Perform training, testing, and/or visualization"""
    global cfg  # If not stated, cfg is seen as local due to in-function assignment.
    if config is not None:
        cfg.merge_from_file(config)
    cfg.merge_from_list(opts)
    # cfg.freeze()
    mlflow.log_params(cfg.flatten())

    logger = utils.setup_logger(cfg.MISC.OUTPUT_DIR)
    #logger.info(cfg)
    logger.info("{} {} {}".format(
        cfg.DATA.DATABASE,
        config,
        cfg.DATA.DATASET,
    ))

    dm = ActiveRegionDataModule(cfg)
    cfg = dm.set_class_weight(cfg)

    if 'train' in modes:
        logger.info("======== TRAIN ========")
        cfg.LEARNER.CHECKPOINT = train(cfg, dm, resume)
        mlflow.set_tag('checkpoint', cfg.LEARNER.CHECKPOINT)

    if 'test' in modes:
        logger.info("======== TEST ========")
        test(cfg, dm)

    #TODO: visualization for LSTM
    #if 'visualize' in args.modes:
    #    logger.info("======== VISUALIZE ========")
    #    visualize(cfg, dm)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('-s', '--smoke', action='store_true',
                        help='Smoke test')
    parser.add_argument('-e', '--experiment_name', default='arnet',
                        help='MLflow experiment name')
    parser.add_argument('-r', '--run_name', default='c3d',
                        help='MLflow run name')
    parser.add_argument('--config', metavar='FILE',
                        help="Path to a yaml formatted config file")
    parser.add_argument('--modes', default='train|test|visualize',
                        help="Perform training, testing, and/or visualization")
    parser.add_argument('--resume', metavar='CHECKPOINT',
                        help="Resume training from checkpoint. Valid only in training mode.")
    parser.add_argument('opts', default=None, nargs=argparse.REMAINDER,
                        help="Modify config options. Use dot(.) to indicate hierarchy.")
    args = parser.parse_args()
    args.modes = args.modes.split('|')
    accepted_modes = ['train', 'test', 'visualize']
    if any([m not in accepted_modes for m in args.modes]):
        raise AssertionError('Mode {} is not accepted'.format(args.modes))
    if 'train' not in args.modes and 'LEARNER.CHECKPOINT' not in args.opts:
        raise ValueError('LEARNER.CHECKPOINT must be specified in the absence of training mode.')
    if args.smoke:
        args.experiment_name = 'smoke_arnet'
        args.opts.extend([
            'TRAINER.limit_train_batches', '10',
            'TRAINER.limit_val_batches', '2',
            'TRAINER.limit_test_batches', '2',
            'TRAINER.max_epochs', '1',
            'TRAINER.default_root_dir', 'lightning_logs_dev'
        ])

    with cProfile.Profile() as p:
        mlflow.set_experiment(experiment_name=args.experiment_name)
        with mlflow.start_run(run_name=args.run_name) as run:
            launch(args.config, args.modes, args.resume, args.opts)
    pstats.Stats(p).sort_stats('cumtime').print_stats(50)


def sweep():
    parser = argparse.ArgumentParser()
    parser.add_argument('-d', '--data_root', default='datasets')
    parser.add_argument('-c', '--config_root', default='arnet/configs')
    parser.add_argument('-s', '--smoke', action='store_true')
    parser.add_argument('-e', '--experiment_name', default='arnet')
    parser.add_argument('-r', '--run_name', default='gamma')
    parser.add_argument('opts', default=None, nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.smoke:
        args.experiment_name = 'smoke_arnet'
        args.opts.extend([
            'TRAINER.limit_train_batches', '5',
            'TRAINER.limit_val_batches', '2',
            'TRAINER.limit_test_batches', '2',
            'TRAINER.max_epochs', '1',
            'TRAINER.default_root_dir', 'lightning_logs_dev'
        ])

    t_start = time.time()
    databases = [p for p in (Path(args.data_root) / 'preprocessed').iterdir() if p.is_dir()]
    databases = [Path(args.data_root) / 'preprocessed' / f'M_Q_{t}hr' for t in [6,12,24]]
    configs = [c for c in Path(args.config_root).iterdir()]
    mlflow.set_experiment(args.experiment_name)
    with mlflow.start_run(run_name=args.run_name):
        for database in databases:
            for balanced in [True, False]:
                for config in configs:
                    for dataset in ['sharp', 'smarp', 'combined']:
                        opts = [
                            'DATA.DATABASE', database,
                            'DATA.DATASET', dataset,
                            'DATA.BALANCED', balanced,
                        ]
                        run_name = '_'.join([database.name, config.stem, dataset])
                        with mlflow.start_run(run_name=run_name, nested=True):
                            tt = time.time()
                            launch(config, 'train|test', False, args.opts + opts)
                            mlflow.log_metric('time', time.time() - tt)
                            mlflow.set_tag('database_name', database.name)
                            mlflow.set_tag('balanced', balanced)
                            mlflow.set_tag('estimator_name', config.stem)
                            mlflow.set_tag('dataset_name', dataset)

    print('Run time: {} s'.format(time.time() - t_start))


if __name__ == '__main__':
    main()
