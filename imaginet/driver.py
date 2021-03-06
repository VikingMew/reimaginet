#!/usr/bin/env python
# encoding: utf-8
# Copyright (c) 2015 Grzegorz Chrupała
# Imaginet (http://arxiv.org/abs/1506.03694) models implemented with funktional
from __future__ import division 
import theano
import numpy
import random
import cPickle as pickle
import argparse
import gzip
import sys
import os
import copy
import funktional.util as util
from funktional.util import linear, clipped_rectify, grouper, autoassign, CosineDistance
from funktional.layer import StackedGRUH0
from collections import Counter
import data_provider as dp
from models import Imaginet, MultitaskLM, predictor_v, predictor_r
import evaluate
from tokens import tokenize
import json
from  sklearn.preprocessing import StandardScaler
import string

class Batcher(object):

    def __init__(self, mapper, pad_end=False, tokenizer='word'):
        autoassign(locals())
        self.BEG = self.mapper.BEG_ID
        self.END = self.mapper.END_ID
        
    def pad(self, xss): # PAD AT BEGINNING
        max_len = max((len(xs) for xs in xss))
        def pad_one(xs):
            if self.pad_end:
                return xs + [ self.END for _ in range(0,(max_len-len(xs))) ] 
            return [ self.BEG for _ in range(0,(max_len-len(xs))) ] + xs
        return [ pad_one(xs) for xs in xss ]

    def batch_inp(self, sents):
        mb = self.padder(sents)
        return mb[:,1:]

    def padder(self, sents):
        return numpy.array(self.pad([[self.BEG]+sent+[self.END] for sent in sents]), dtype='int32')

    def batch(self, item):
        """Prepare minibatch."""
        mb_inp = self.padder([s for s,_,_ in item])
        mb_out_t = self.padder([r for _,r,_ in item])
        inp = mb_inp[:,1:]
        out_t = mb_out_t[:,1:]
        out_prev_t = mb_out_t[:,0:-1]
        out_v = numpy.array([ t for _,_,t in item ], dtype='float32')
        return (inp, out_v, out_prev_t, out_t)
    
def valid_loss(model, data):
    """Apply model to validation data and return loss info."""
    c = Counter()
    for item in data.iter_valid_batches():
        inp, out_v, out_prev_t, out_t = item
        cost, cost_t, cost_v = model.loss_test(inp, out_v, out_prev_t, out_t)
        c += Counter({'cost_t': cost_t, 'cost_v': cost_v, 'cost': cost, 'N': 1})
    return c
    
class NoScaler():
    def __init__(self):
        pass
    def fit_transform(self, x):
        return x
    def transform(self, x):
        return x
    def inverse_transform(self, x):
        return x

def stats(c):
    return " ".join(map(str, [c['cost_t']/c['N'], c['cost_v']/c['N'], c['cost']/c['N']]))

class Data(object):
    """Training / validation data prepared to feed to the model."""
    def __init__(self, provider, mapper, scaler, batch_size=64, with_para='auto', shuffle=False,
                 fit=True, pad_end=False, reverse=False, tokenizer='word'):
        autoassign(locals())
        self.data = {}
        # TRAINING
        if self.with_para == 'para_rand':
            sents_in, sents_out, imgs = zip(*self.shuffled(arrange_para_rand(provider.iterImages(split='train'),
                                                                             reverse=self.reverse,
                                                                             tokenizer=self.tokenizer
                                                                         )))
        elif self.with_para == 'auto':
            sents_in, sents_out, imgs = zip(*self.shuffled(arrange_auto(provider.iterImages(split='train'),
                                                                        reverse=self.reverse,
                                                                        tokenizer=self.tokenizer
                                                                    )))
        elif self.with_para == 'para_all':
            sents_in, sents_out, imgs = zip(*self.shuffled(arrange_para(provider.iterImages(split='train'),
                                                                        reverse=self.reverse,
                                                                        tokenize=self.tokenizer
                                                                    )))
        else:
            raise ValueError("Unknown value for keyword argument 'with_para': {}".format(self.with_para))

        if self.fit:
            sents_in = self.mapper.fit_transform(sents_in)
            imgs = self.scaler.fit_transform(imgs)
        else:
            sents_in = self.mapper.transform(sents_in)
            imgs = self.scaler.transform(imgs)
            
        sents_out = self.mapper.transform(sents_out)

        self.data['train'] = zip(sents_in, sents_out, imgs)
        # VALIDATION
        if self.with_para == 'para_rand':
            sents_in, sents_out, imgs = zip(*self.shuffled(arrange_para_rand(provider.iterImages(split='val'),
                                                                             reverse=self.reverse,
                                                                             tokenizer=self.tokenizer)))
        elif self.with_para == 'auto':
            sents_in, sents_out, imgs = zip(*self.shuffled(arrange_auto(provider.iterImages(split='val'),
                                                                        reverse=self.reverse,
                                                                        tokenizer=self.tokenizer)))
        elif self.with_para == 'para_all':
            sents_in, sents_out, imgs = zip(*self.shuffled(arrange_para(provider.iterImages(split='val'),
                                                                        reverse=self.reverse,
                                                                        tokenizer=self.tokenizer)))

        sents_in = self.mapper.transform(sents_in)
        sents_out = self.mapper.transform(sents_out)
        imgs = self.scaler.transform(imgs)
        self.data['valid'] = zip(sents_in, sents_out, imgs)
        self.batcher = Batcher(self.mapper, self.pad_end, tokenizer=self.tokenizer)
        
    def shuffled(self, xs):
        if not self.shuffle:
            return xs
        else:
            zs = copy.copy(list(xs))
            random.shuffle(zs)
            return zs
        
    def iter_train_batches(self):
        for bunch in grouper(self.data['train'], self.batch_size*20):
            bunch_sort = [ bunch[i] for i in numpy.argsort([len(x) for x,_,_ in bunch]) ]
            for item in grouper(bunch_sort, self.batch_size):
                yield self.batcher.batch(item)
        
    def iter_valid_batches(self):
        for bunch in grouper(self.data['valid'], self.batch_size*20):
            bunch_sort = [ bunch[i] for i in numpy.argsort([len(x) for x,_,_ in bunch]) ]
            for item in grouper(bunch_sort, self.batch_size):
                yield self.batcher.batch(item)

    def dump(self, model_path):
        """Write mapper and scaler to disc."""
        pickle.dump(self.scaler, gzip.open(os.path.join(model_path, 'scaler.pkl.gz'), 'w'),
                    protocol=pickle.HIGHEST_PROTOCOL)
#        pickle.dump(self.mapper, gzip.open(os.path.join(model_path, 'mapper.pkl.gz'),'w'),
#                    protocol=pickle.HIGHEST_PROTOCOL)
        pickle.dump(self.batcher, gzip.open(os.path.join(model_path, 'batcher.pkl.gz'), 'w'),
                    protocol=pickle.HIGHEST_PROTOCOL)

def tokens(sent, tokenizer='word'):
    if tokenizer == 'word':
        return tokenize(sent['raw'])
    elif tokenizer == 'word-clean':
        return sent['tokens']
    elif tokenizer == 'char':
        return list(sent['raw'])
    elif tokenizer == 'phon':
        # remove spaces/punctuation, and lowercase
        return [ c.lower() for c in sent['raw'] if c in string.letters ]
        
def arrange_para_rand(data, reverse=False, tokenizer='word'):
    for image in data:
        for sent_in in image['sentences']:
            sent_out = random.choice(image['sentences'])
            yield (tokens(sent_in, tokenizer=tokenizer),
                   list(reversed(tokens(sent_out, tokenizer=tokenizer))) if reverse else tokens(sent_out, tokenizer=tokenizer), image['feat'])

def arrange_para(data, reverse=False, tokenizer='word'):
    for image in data:
        for sent_in in image['sentences']:
            for sent_out in image['sentences']:
                yield (tokens(sent_in, tokenizer=tokenizer),
                       list(reversed(tokens(sent_out, tokenizer=tokenizer))) if reverse else tokens(send_out, tokenizer=tokenizer), image['feat'])
                
def arrange_auto(data, reverse=False, tokenizer='word'):
    for image in data:
        for sent in image['sentences']:
            yield (tokens(sent, tokenizer=tokenizer),
                   list(reversed(tokens(sent, tokenizer=tokenizer))) if reverse else tokens(sent, tokenizer=tokenizer), image['feat'])

            
def cmd_train_resume( dataset='coco',
                      extra_train=False,
                      datapath='.',
                      model_path='.',
                      model_name='model.pkl.gz',
                      seed=None,
                      shuffle=False,
                      with_para='auto',
                      start_epoch=1,
                      epochs=1,
                      batch_size=64,
                      validate_period=64*100,
                      logfile='log.txt'):
    def load(f):
        return pickle.load(gzip.open(os.path.join(model_path, f)))
    sys.setrecursionlimit(50000)
    if seed is not None:
        random.seed(seed)
        numpy.random.seed(seed)
    prov = dp.getDataProvider(dataset, root=datapath, extra_train=extra_train)
    batcher, scaler, model = map(load, ['batcher.pkl.gz', 'scaler.pkl.gz', model_name])
    data = Data(prov, batcher.mapper, scaler, batch_size=batch_size, with_para=with_para,
                shuffle=shuffle, fit=False)
    do_training(logfile, epochs, start_epoch, batch_size, validate_period, model_path, model, data)
                      
def cmd_train( dataset='coco',
               extra_train=False,
               datapath='.',
               model_path='.',
               hidden_size=1024,
               gru_activation=clipped_rectify,
               visual_activation=linear,
               visual_encoder=StackedGRUH0,
               max_norm=None,
               lr=0.0002,
               embedding_size=None,
               depth=1,
               grow_depth=None,
               grow_params_path=None,
               scaler=None,
               cost_visual=CosineDistance,
               seed=None,
               shuffle=False,
               reverse=False,
               with_para='auto',
               tokenizer='word',
               architecture=MultitaskLM,
               dropout_prob=0.0,
               alpha=0.1,
               epochs=1,
               batch_size=64,
               pad_end=False,
               validate_period=64*100,
               logfile='log.txt'):
    sys.setrecursionlimit(50000) # needed for pickling models
    if seed is not None:
        random.seed(seed)
        numpy.random.seed(seed)
    prov = dp.getDataProvider(dataset, root=datapath, extra_train=extra_train)
    mapper = util.IdMapper(min_df=10)
    embedding_size = embedding_size if embedding_size is not None else hidden_size
    scaler = StandardScaler() if scaler == 'standard' else NoScaler()
    data = Data(prov, mapper, scaler, batch_size=batch_size, with_para=with_para,
                shuffle=shuffle, reverse=reverse, tokenizer=tokenizer)
    data.dump(model_path)
    model = Imaginet(size_vocab=mapper.size(),
                     size_embed=embedding_size,
                     size=hidden_size,
                     size_out=4096,
                     depth=depth,
                     network=architecture,
                     cost_visual=cost_visual,
                     alpha=alpha,
                     gru_activation=gru_activation,
                     visual_activation=visual_activation,
                     visual_encoder=visual_encoder,
                     max_norm=max_norm,
                     lr=lr,
                     dropout_prob=dropout_prob)
    start_epoch=1
    grow_depth = depth if grow_depth is None else grow_depth
    do_training(logfile, epochs, start_epoch, batch_size, validate_period, model_path, model, data, grow_depth, grow_params_path)
    
def do_training(logfile, epochs, start_epoch, batch_size, validate_period, model_path, model, data, grow_depth, grow_params_path):
    with open(logfile, 'w') as log:
        current_depth=model.depth
        for epoch in range(start_epoch, epochs + 1):
            if epoch > 1 and current_depth < grow_depth:
                ps = numpy.load(grow_params_path)
                model.grow(ps)
                current_depth += 1

            print len(model.network.params())
            costs = Counter()
            N = 0
            # recent = []
            for _j, item in enumerate(data.iter_train_batches()):
                j = _j + 1
                inp, out_v, out_prev_t, out_t = item
                cost, cost_t, cost_v = model.train(inp, out_v, out_prev_t, out_t)
                #grads =  model.delete_me(inp, out_v, out_prev_t, out_t)
                #for grad in grads:
                #    print grad.shape
                #    print numpy.linalg.norm(grad)
                costs += Counter({'cost_t':cost_t, 'cost_v': cost_v, 'cost': cost, 'N': 1})
                #recent = recent[-5:];recent.append(costs['cost']/costs['N'])
                print epoch, j, j*batch_size, "train", stats(costs)
                # check if cost diverges
                # if len(recent) >= 5 and recent[-1] > recent[0]:
                #     model.updater.lr = model.updater.lr / 2
                #     print epoch, j, j*batch_size, "lrate", model.updater.lr, recent
                #     recent = []
                 
                if j*batch_size % validate_period == 0:
                    costs_valid = valid_loss(model, data)
                    print epoch, j, j, "valid", stats(costs_valid)
                sys.stdout.flush()
            pickle.dump(model, gzip.open(os.path.join(model_path, 'model.{0}.pkl.gz'.format(epoch)),'w'),
                        protocol=pickle.HIGHEST_PROTOCOL)
        pickle.dump(model, gzip.open(os.path.join(model_path, 'model.pkl.gz'), 'w'),
                    protocol=pickle.HIGHEST_PROTOCOL)

def load(d, model_name='model.pkl.gz'):
    def load_pklgz(f):
        return pickle.load(gzip.open(os.path.join(d, f)))
    batcher, scaler, model = map(load_pklgz, ['batcher.pkl.gz','scaler.pkl.gz', model_name])
    mapper = batcher.mapper
    return {'batcher': batcher, 'scaler': scaler, 'model': model }

def batch_sents(batcher, sents):
    return batcher.batch_inp(list(batcher.mapper.transform([ tokenize(s) for s in sents ])))

def cmd_predict_v(dataset='coco',
                  datapath='.',
                  model_path='.',
                  model_name='model.pkl.gz',
                  batch_size=128,
                  output_v='predict_v.npy',
                  output_r='predict_r.npy'):
    M = load(model_path, model_name=model_name)
    model = M['model']
    batcher = M['batcher']
    mapper = M['batcher'].mapper
    predict_v = predictor_v(model)
    predict_r = predictor_r(model)
    prov   = dp.getDataProvider(dataset, root=datapath)
    sents  = list(prov.iterSentences(split='val'))
    inputs = list(mapper.transform([tokens(sent, tokenizer=batcher.tokenizer) for sent in sents ]))
    print len(model.network.params())
    preds_v  = numpy.vstack([ predict_v(batcher.batch_inp(batch))
                            for batch in grouper(inputs, batch_size) ])
    numpy.save(os.path.join(model_path, output_v), preds_v)
    preds_r = numpy.vstack([ predict_r(batcher.batch_inp(batch))
                             for batch in grouper(inputs, batch_size) ])
    numpy.save(os.path.join(model_path, output_r), preds_r)
    
def cmd_eval(dataset='coco',
             datapath='.',
             scaler_path='scaler.pkl.gz',
             input_v='predict_v.npy',
             input_r='predict_r.npy',
             output='eval.json'):
    scaler = pickle.load(gzip.open(scaler_path))
    preds_v  = numpy.load(input_v)
    preds_r  = numpy.load(input_r)
    prov   = dp.getDataProvider(dataset, root=datapath)
    sents  = list(prov.iterSentences(split='val'))
    images = list(prov.iterImages(split='val'))
    img_fs = list(scaler.transform([ image['feat'] for image in images ]))
    correct_img = numpy.array([ [ sents[i]['imgid']==images[j]['imgid']
                              for j in range(len(images)) ]
                            for i in range(len(sents)) ])
    correct_para = numpy.array([ [ sents[i]['imgid'] == sents[j]['imgid']
                               for j in range(len(sents)) ]
                            for i in range(len(sents)) ])
    r_img = evaluate.ranking(img_fs, preds_v, correct_img, ns=(1,5,10), exclude_self=False)
    r_para_v = evaluate.ranking(preds_v, preds_v, correct_para, ns=(1,5,10), exclude_self=True)
    r_para_r  = evaluate.ranking(preds_r, preds_r, correct_para, ns=(1,5,10), exclude_self=True)
    r = {'img':r_img, 'para_v':r_para_v,'para_r':r_para_r }
    json.dump(r, open(output, 'w'))
    for mode in ['img', 'para_v', 'para_r']:
        print '{} median_rank'.format(mode), numpy.median(r[mode]['ranks'])
        for n in (1,5,10):
            print '{} recall@{}'.format(mode, n), numpy.mean(r[mode]['recall'][n])
            sys.stdout.flush()

