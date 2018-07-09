import json
import os
import numpy as np
from pycocotools.coco import COCO
from pycocoevalcap.eval import COCOEvalCap
from tensorflow.python.keras.utils import Progbar
from tensorflow.python.keras import backend as K
import model as M
import args as P
from utils import config
from utils.vocabulary import (
    preprocess_captions, captions_to_tokens, caption_tokens_to_id
)
from utils.dataloader import COCOSequence
from utils.language import predictions_to_captions

val_captions_file_1k = 'captions_val2014_1k.json'


def coco_metrics(args, results_file):
  coco = COCO(val_captions_file_1k)
  cocoRes = coco.loadRes(results_file)
  cocoEval = COCOEvalCap(coco, cocoRes)
  cocoEval.evaluate()
  return cocoEval.eval[args.es_metric]


def generate_captions(args, model, generator, vocab):
  captions = []
  # HACK: Track image_ids so we do not generate duplicates
  img_ids = []
  gen = generator.once(True, samples=args.val_samples)
  for (img, input_caption), _, _, image_ids in gen:
    if args.es_prev_words == 'gt':
      preds = model.predict_on_batch([img, input_caption])
      word_idxs = np.argmax(preds, axis=-1)
    else:
      prevs = np.zeros((args.bs, 1))
      word_idxs = np.zeros((args.bs, args.seqlen))
      for i in range(args.seqlen):
        # get predictions
        preds = model.predict_on_batch([img, prevs])
        preds = preds.squeeze()
        word_idxs[:, i] = np.argmax(preds, axis=-1)
        prevs = np.argmax(preds, axis=-1)
        prevs = np.reshape(prevs, (args.bs, 1))
    model.reset_states()

    caps = predictions_to_captions(word_idxs, vocab)
    for i, caption in enumerate(caps):
      if i > len(image_ids) - 1:
        # Last batch might be smaller
        break
      img_id = int(image_ids[i])
      if img_id in img_ids:
        continue
      img_ids.append(img_id)
      caption = ' '.join(caption)  # exclude eos
      captions.append({"image_id": img_id,
                       "caption": caption.split('<eos>')[0]})

  results_file = os.path.join(args.data_folder, 'results',
                              args.model_name + '_gencaps_val.json')
  os.makedirs(os.path.dirname(results_file), exist_ok=True)
  with open(results_file, 'w+') as outfile:
    json.dump(captions, outfile)

  return results_file


def trainloop(args, model, validation_model=None, epoch_start=0, suffix=''):
  train_coco = COCO(args.train_coco_file)
  val_coco = COCO(args.val_coco_file)
  vocab, vocab_size, counts = preprocess_captions(
      [train_coco.anns, val_coco.anns], args.word_count_threshold,
      args.tokenizer)
  val_coco = COCO(val_captions_file_1k)
  captions_to_tokens([val_coco.anns], args.tokenizer)
  caption_tokens_to_id([val_coco.anns], vocab, counts,
                       args.word_count_threshold)
  train = COCOSequence(
      args.train_img_dir, train_coco, vocab_size, args.seqlen, args.bs,
      args.imgw, args.imgh, args.preprocessed)
  validation = COCOSequence(
      args.val_img_dir, val_coco, vocab_size, args.seqlen, args.bs,
      args.imgw, args.imgh, args.preprocessed)

  itow = {i + 1: w for i, w in enumerate(vocab)}

  wait = 0
  best_metric = -np.inf
  for e in range(epoch_start, args.nepochs):
    print("Epoch {}/{}".format(e + 1 + epoch_start, args.nepochs + epoch_start))
    prog = Progbar(target=len(train))

    for i, (x, y, sw) in enumerate(train.once(samples=args.train_samples)):
      loss = model.train_on_batch(x=x, y=y, sample_weight=sw)
      model.reset_states()
      prog.update(current=i + 1, values=[('loss', loss)])

    print('Validation')
    val_prog = Progbar(target=len(validation))
    val_losses = []
    for i, (x, y, sw) in enumerate(validation.once(samples=args.val_samples)):
      val_losses.append(model.test_on_batch(x, y, sw))
      model.reset_states()
      val_prog.update(current=i)

    print()
    aux_model = os.path.join(args.data_folder, 'tmp',
                             args.model_name + '_aux.h5')
    os.makedirs(os.path.dirname(aux_model), exist_ok=True)
    model.save_weights(aux_model, overwrite=True)
    validation_model.load_weights(aux_model)
    results_file = generate_captions(args, validation_model, validation, itow)

    metric = coco_metrics(args, results_file)

    if metric > best_metric:
      print('Model improved from {} to {}'.format(best_metric, metric))
      best_metric = metric
      wait = 0
      model_name = os.path.join(
          args.data_folder, 'models',
          args.model_name + suffix + '_weights_e' + str(e) + '_' +
          args.es_metric + "{:.2f}".format(metric) + '.h5')
      os.makedirs(os.path.dirname(model_name), exist_ok=True)
      model.save_weights(model_name)
    else:
      wait += 1

    if wait > args.patience:
      print('Waited too long. Stopping training!')
      break

  model_name = os.path.join(
      args.data_folder, 'models',
      args.model_name + suffix + '_weights_e' + str(e) + '_lang_finished.h5')
  os.makedirs(os.path.dirname(model_name), exist_ok=True)
  model.save_weights(model_name)
  return model, model_name


def init_models(args, cnn_train=False):
  model = M.get_model(args, cnn_train)
  optimizer = config.get_optimizer(args)
  model.compile(optimizer=optimizer, loss='categorical_crossentropy',
                sample_weight_mode="temporal")

  # Validation Model for EarlyStopping
  if not args.es_metric == 'loss':
    args.mode = 'test'
    validation_model = M.get_model(args)
    validation_model.compile(
        optimizer=optimizer, loss='categorical_crossentropy',
        sample_weight_mode="temporal")
    args.mode = 'train'
  else:
    validation_model = None

  return model, validation_model


if __name__ == '__main__':
  parser = P.get_parser()
  args = parser.parse_args()

  if args.gpus > 1:
    args.model_bs = args.bs // args.gpus
  else:
    args.model_bs = args.bs

  model_name = None

  # Train Language Model
  if args.current_lang_epoch < args.lang_epochs:
    model, validation_model = init_models(args)
    if args.model_file:
      model.load_weights(args.model_file)
    _, model_name = trainloop(args, model, validation_model,
                              epoch_start=args.current_lang_epoch)
    del model
    K.clear_session()

  if args.current_cnn_epoch < args.cnn_epochs:
    model, validation_model = init_models(args, cnn_train=True)
    if model_name:
      model.load_weights(model_name)
    elif args.model_file:
      model.load_weights(args.model_file)
    else:
      raise ValueError('Finetuning cnn without trained language model!')
    trainloop(args, model, suff_name='_cnn_train', model_val=validation_model,
              epoch_start=args.current_cnn_epoch)
