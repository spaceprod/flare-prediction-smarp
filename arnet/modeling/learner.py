import os
import logging
from typing import Dict, Union
from datetime import timedelta

import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import pandas as pd
import mlflow
import torch
import pytorch_lightning as pl
import pprint
pp = pprint.PrettyPrinter(indent=4)

from arnet import utils
from arnet.modeling.models import build_model

logger = logging.getLogger(__name__)


def build_test_logger(logged_learner, testmode):
    logger = pl.loggers.TensorBoardLogger(logged_learner.logger_save_dir,
                                          name=logged_learner.logger_name,
                                          version=logged_learner.logger_version + '_' + testmode)
    return logger


class Learner(pl.LightningModule):
    def __init__(self, cfg):
        """
        model: torch.nn.Module
        cfg: model-agnostic experiment configs
        """
        super(Learner, self).__init__()
        self.cfg = cfg
        self.image = 'MAGNETOGRAM' in cfg.DATA.FEATURES
        self.model = build_model(cfg)
        self.testmode = 'test'
        self.save_hyperparameters() # write to self.hparams. when save model, they are # responsible for tensorboard hp_metric

    def forward(self, *args, **kwargs):
        return self.model(*args, **kwargs)

    def on_load_checkpoint(self, checkpoint) -> None:
        ckpt_dict = checkpoint['callbacks'][pl.callbacks.model_checkpoint.ModelCheckpoint]
        log_dir = os.path.dirname(ckpt_dict['dirpath'])
        root_dir, version = os.path.split(log_dir)
        save_dir, name = os.path.split(root_dir)
        #  log_dev / lightning_logs / version_0 / checkpoints
        # =======================================
        # save_dir /    (name)        (version)
        # ------- root_dir ---------/
        # ------------ log_dir ----------------/
        self.logger_save_dir, self.logger_name, self.logger_version = (
            save_dir, name, version)

    def grad_norm(self, norm_type: Union[float, int, str]) -> Dict[str, float]:
        """Compute each parameter's gradient's norm and their overall norm.

        The overall norm is computed over all gradients together, as if they
        were concatenated into a single vector.

        Args:
            norm_type: The type of the used p-norm, cast to float if necessary.
                Can be ``'inf'`` for infinity norm.

        Return:
            norms: The dictionary of p-norms of each parameter's gradient and
                a special entry for the total p-norm of the gradients viewed
                as a single vector.
        """
        #norm_type = float(norm_type)

        norms, all_norms = {}, []
        for name, p in self.named_parameters():
            if name.split('.')[0] == 'model':
                name = name[6:]

            if p.grad is None:
                continue

            param_norm = float(p.data.norm(norm_type))
            grad_norm = float(p.grad.data.norm(norm_type))
            norms[f'grad_{norm_type}_norm/{name}'] = {
                'param': param_norm,
                'grad': grad_norm,
            }

            all_norms.append(param_norm)

        total_norm = float(torch.tensor(all_norms).norm(norm_type))
        norms[f'grad_{norm_type}_norm/total'] = round(total_norm, 3)

        return norms

    def _check_nan_loss(self, loss):
        if torch.isnan(loss):
            norms = self.grad_norm(1)
            import json
            print(json.dumps(norms, indent=2))

    def training_step(self, batch, batch_idx):
        loss = self.model.get_loss(batch)
        self._check_nan_loss(loss)

        # Scalar(s)
        self.log('train/loss', loss)
        mlflow.log_metric('train/loss', loss.item(), step=self.global_step)
        mlflow.log_metric('train/epoch', self.trainer.current_epoch, step=self.global_step)

        if self.image:
            # Text
            if self.global_step in [0] or batch_idx == 0:
                self.log_meta(self.model.result, model_type=self.model.mode)

            # Input videos (padded)
            if False: #self.global_step in [0] or batch_idx == 0:
                self.log_video('train/inputs', x)

            # Layer weight
            if self.current_epoch == 0 and batch_idx in [0, 1, 2, 5, 10, 20, 50, 100]:
                self.log_layer_weights('weight', ['convs.conv1'])

            # Middle layer features
            if False: #self.global_step in [0] or batch_idx == 0:
                self.log_layer_activations('train features', self.model.result['video'], self.cfg.LEARNER.VIS.ACTIVATIONS)

            # Weight histograms
            if True: #self.global_step in [0] or batch_idx == 0:
                for layer_name in self.cfg.LEARNER.VIS.HISTOGRAM:
                    self.logger.experiment.add_histogram("weights/{} kernel".format(layer_name),
                        utils.get_layer(self.model, layer_name).weight, self.global_step)

        self.logger.experiment.flush()
        return {'loss': loss}

    def validation_step(self, batch, batch_idx):
        loss = self.model.get_loss(batch)

        result = self.model.result
        result.update({'val_loss': loss})
        return result

    def validation_epoch_end(self, outputs):
        avg_val_loss = torch.stack([out['val_loss'] for out in outputs]).mean()
        self.log('validation/loss', avg_val_loss)
        mlflow.log_metric('validation/loss', avg_val_loss.item(), step=self.global_step)

        if True:
            step = -1 if self.global_step == 0 else None # before training
            self.log_layer_weights('weight', ['convs.conv1'], step=step)

        if self.model.mode == 'classification':
            y_true = torch.cat([out['y_true'] for out in outputs])
            y_prob = torch.cat([out['y_prob'] for out in outputs])
            self.trainer.datamodule.fill_prob('validation', self.global_step, y_prob.detach().cpu().numpy())
            scores, cm2, _ = utils.get_metrics_probabilistic(y_true, y_prob, criterion=None)
            self.log_scores('validation', scores, step=self.global_step) # pp.pprint(scores)
            self.log_cm('validation/cm2', cm2, step=self.global_step)
            self.log_eval_plots('validation', y_true, y_prob, step=self.global_step)
        elif self.model.mode == 'regression':
            i_true = torch.cat([out['i_true'] for out in outputs])
            i_pred = torch.cat([out['i_pred'] for out in outputs])
            scores, cm6, cm2, cmq = utils.get_metrics_multiclass(i_true, i_pred)
            self.log_scores('validation', scores, step=self.global_step)
            self.log_cm('validation/cm6', cm6, labels=['Q', 'A', 'B', 'C', 'M', 'X'], step=self.global_step)
            self.log_cm('validation/cm2', cm2, step=self.global_step)
            self.log_cm('validation/cmq', cmq, step=self.global_step)
        else:
            raise ValueError
        mlflow.log_artifacts(self.logger.log_dir, 'tensorboard/train_val')

    def test_step(self, batch, batch_idx):
        if self.testmode == 'test':
            loss = self.model.get_loss(batch)
            result = self.model.result
            result.update({'test_loss': loss})
            return result

        elif self.testmode == 'visualize_predictions':
            video, size, target, meta = batch
            gradcam = utils.GradCAM(self.model,
                                    target_layers=self.cfg.LEARNER.VIS.GRADCAM_LAYERS,
                                    data_mean=0,
                                    data_std=1)
            video_gradcam, video_scaled, heatmap, output = gradcam(video)
            loss = self.model.get_loss(batch)

            self.log_video('visualize/inputs/gradcam_input', video_scaled, size=size, normalized=True, step=batch_idx)
            self.log_video('visualize/inputs/gradcam_mask', heatmap, size=size, normalized=True, step=batch_idx)
            self.log_video('visualize/inputs/gradcam', video_gradcam, size=size, normalized=True, step=batch_idx)
            info = self.log_meta(self.model.result, model_type=self.model.mode)
            vid_start_time = pd.to_datetime(info['start_time'], format='%Y%m%d%H%M%S')
            forecast_issuance_time = vid_start_time + timedelta(hours=24)

            result = self.model.result
            result.update({'test_loss': loss, 'issuance': forecast_issuance_time})
            return result

        elif self.testmode == 'visualize_features':
            video, size, target, meta = batch
            _ = self.model(video)

            self.log_video('visualize/inputs/full', video, size=size, step=batch_idx)
            self.log_layer_activations('visualize_features', video, self.cfg.LEARNER.VIS.ACTIVATIONS, step=batch_idx)

    def test_epoch_end(self, outputs):
        if self.testmode == 'test':
            avg_test_loss = torch.stack([out['test_loss'] for out in outputs]).mean()
            self.log('test/loss', avg_test_loss)
            if self.model.mode == 'classification':
                y_true = torch.cat([out['y_true'] for out in outputs])
                y_prob = torch.cat([out['y_prob'] for out in outputs])
                self.trainer.datamodule.fill_prob('test', self.global_step, y_prob.detach().cpu().numpy())
                scores, cm2, thresh = utils.get_metrics_probabilistic(y_true, y_prob, criterion=None)
                #self.thresh = thresh
                logger.info(scores)
                logger.info(cm2)
                self.log_scores('test', scores)
                self.log_cm('test/cm2', cm2)
                self.log_eval_plots('test', y_true, y_prob)
            elif self.model.mode == 'regression':
                i_true = torch.cat([out['i_true'] for out in outputs])
                i_pred = torch.cat([out['i_pred'] for out in outputs])
                scores, cm6, cm2, cmq = utils.get_metrics_multiclass(i_true, i_pred)
                self.log_scores('test', scores)
                self.log_cm('test/cm6', cm6, labels=['Q', 'A', 'B', 'C', 'M', 'X'])
                self.log_cm('test/cm2', cm2)
                self.log_cm('test/cmq', cmq)
        elif self.testmode == 'visualize_predictions':
            forecast_issuance_time = pd.concat([out['issuance'] for out in outputs]).tolist()
            filename = os.path.join(self.logger.log_dir, 'temp.png')
            if self.model.mode == 'classification':
                y_true = torch.cat([out['y_true'] for out in outputs])
                y_prob = torch.cat([out['y_prob'] for out in outputs])
                utils.plot_prediction_curve(noaa_ar=11158,
                                            times=forecast_issuance_time,
                                            y_prob=y_prob.tolist(),
                                            y_true=y_true.tolist(),
                                            filename=filename)
            elif self.model.mode == 'regression':
                i_true = torch.cat([out['i_true'] for out in outputs])
                i_pred = torch.cat([out['i_pred'] for out in outputs])
                utils.plot_prediction_curve(noaa_ar=11158,
                                            times=forecast_issuance_time,
                                            y_prob=i_pred.tolist(),
                                            y_true=i_true.tolist(),
                                            filename=filename)
        elif self.testmode == 'visualize_features':
            pass
        else:
            raise ValueError
        mlflow.log_artifacts(self.logger.log_dir, 'tensorboard/test')

    def predict_step(self, batch, batch_idx: int , dataloader_idx: int = None):
        _ = self.model.get_loss(batch)
        y_prob = self.model.result['y_prob']
        ###
        #self.thresh = 0.5
        ###
        return y_prob #y_prob >= 0.5 #self.thresh

    def configure_optimizers(self):
        return torch.optim.Adam(self.parameters(), lr=self.cfg.LEARNER.LEARNING_RATE)

    def on_train_end(self):
        self.trainer.datamodule.df_val_pred.to_csv('outputs/val_pred.csv')
        mlflow.log_artifact('outputs/val_pred.csv', 'validation/predictions.csv')

    def on_test_end(self):
        self.trainer.datamodule.df_test_pred.to_csv('outputs/test_pred.csv')
        mlflow.log_artifact('outputs/test_pred.csv', 'test/predictions.csv')

    def log_meta(self, outputs, model_type='classification', step=None):
        video = outputs['video']
        meta = outputs['meta']
        video = video.detach().cpu().numpy()
        if model_type == 'classification':
            y_true = outputs['y_true'].detach().cpu().numpy()
            y_prob = outputs['y_prob'].detach().cpu().numpy()
            info = utils.generate_batch_info_classification(video, meta, y_true=y_true, y_prob=y_prob)
        elif model_type == 'regression':
            i_true = outputs['i_true'].detach().cpu().numpy()
            i_hat = outputs['i_hat'].detach().cpu().numpy()
            q_prob = outputs['q_prob'].detach().cpu().numpy()
            info = utils.generate_batch_info_regression(video, meta, i_true=i_true, i_hat=i_hat, q_prob=q_prob)
        else:
            raise ValueError
        step = step or self.global_step
        self.logger.experiment.add_text("batch info", info.to_markdown(), step)
        return info

    def log_video(self, tag, video, size=None, normalized=False, step=None):
        from skimage.transform import resize
        size = np.round(size.detach().cpu().numpy() * [38, 78] + [78, 157]).astype(int)

        # video: [N, C, T, H, W]
        if video.shape[0] > 8:
            video = video[:8]
        video = video.detach().permute(0, 2, 1, 3, 4).to('cpu', non_blocking=True)  # convert to numpy may not be efficient in production
        # (N,C,D,H,W) -> (N,T,C,H,W)
        step = step or self.global_step
        if not normalized:
            video = utils.array_to_float_video(video * 50, low=-200, high=200, perc=False)
        self.logger.experiment.add_video(tag, video, step, fps=10)
        vs = video.detach().cpu().numpy()
        for i, v in enumerate(vs):
            for j, image in enumerate(v):
                image = image.transpose(1,2,0)
                if size is not None:
                    image = resize(image, size[i])
                mlflow.log_image(image, tag+f'/{i}_{j}.png')

    def log_layer_weights(self, tag, layer_names, step=None):
        step = step or self.global_step
        from arnet.modeling.models import MODEL_REGISTRY
        if isinstance(self.model, MODEL_REGISTRY.get('CNN_Li2020')):
            for layer_name in layer_names:
                layer = utils.get_layer(self.model, layer_name)
                if isinstance(layer, torch.nn.Conv3d):
                    fig = utils.draw_conv2d_weight(layer.weight)
                    image_tensor = utils.fig2rgb(fig)
                    save_name = tag + f'/{layer_name}_weight_{step}'
                    self.logger.experiment.add_image(save_name, image_tensor, step)
                    mlflow.log_figure(fig, save_name + '.png')

    def log_layer_activations(self, tag, x, layer_names, step=None):
        step = step or self.global_step
        import copy
        model = copy.copy(self.model) # shallow copy, the original model keeps training mode and no activation hook attached
        activations = utils.register_activations(model, layer_names)
        model.eval()
        _ = self.model(x)
        for layer_name in activations:
            features = activations[layer_name].detach().cpu()
            if features.shape[0] > 8:
                features = features[:8]
            for c in range(features.shape[1]):
                features_c = features[:,[c],:,:,:].permute(0,2,1,3,4)
                features_c = utils.array_to_float_video(features_c, 0.1, 99.9)
                self.logger.experiment.add_video(
                    '{}/{}/ch{}'.format(tag, layer_name, c),
                    features_c,
                    step)

    def log_scores(self, tag, scores: dict, step=None):
        step = step or self.global_step
        for k, v in scores.items():
            #self.logger.experiment.add_scalar(tag + '/' + k, v, step)
            self.log(tag + '/' + k, v) #wield problem
        mlflow.log_metrics({tag + '/' + k: v.item() for k, v in scores.items()},
                           step=step)

    def log_cm(self, tag, cm, labels=None, step=None):
        fig = utils.draw_confusion_matrix(cm.cpu())
        image_tensor = utils.fig2rgb(fig)
        self.logger.experiment.add_image(tag, image_tensor, step)
        mlflow.log_figure(fig, tag + f'/confusion_matrix_{step}.png')

    def log_eval_plots(self, tag, y_true, y_prob, step=None):
        y_true = y_true.detach().cpu()
        y_prob = y_prob.detach().cpu()
        step = step or self.global_step

        reliability = utils.draw_reliability_plot(y_true, y_prob, n_bins=10)
        mlflow.log_figure(reliability, tag + f'/reliability_{step}.png')
        reliability = utils.fig2rgb(reliability)
        self.logger.experiment.add_image(tag + '/reliability', reliability, step)

        roc = utils.draw_roc(y_true, y_prob)
        mlflow.log_figure(roc, tag + f'/roc_{step}.png')
        roc = utils.fig2rgb(roc)
        self.logger.experiment.add_image(tag + '/roc', roc, step)

        ssp = utils.draw_ssp(y_true, y_prob)
        mlflow.log_figure(ssp, tag + f'/ssp_{step}.png')
        ssp = utils.fig2rgb(ssp)
        self.logger.experiment.add_image(tag + '/ssp', ssp, step)
