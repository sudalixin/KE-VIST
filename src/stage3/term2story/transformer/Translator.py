''' This module will handle the text generation with beam search. '''

import torch
import torch.nn as nn
import torch.nn.functional as F

from transformer.Models import Transformer
from transformer.Beam import Beam
from transformer import Constants
import math
class Translator(object):
    ''' Load with trained model and handle the beam search '''

    def __init__(self, opt):
        self.opt = opt
        self.device = torch.device(f'cuda:{opt.device}' if opt.cuda else 'cpu')

        checkpoint = torch.load(opt.model, map_location=self.device)
        model_opt = checkpoint['settings']
        model_opt.max_encode_token_seq_len=51
        self.model_opt = model_opt
        model = Transformer(
            model_opt.src_vocab_size,
            model_opt.tgt_vocab_size,
            model_opt.max_encode_token_seq_len,
            model_opt.max_token_seq_len,
            tgt_emb_prj_weight_sharing=model_opt.proj_share_weight,
            emb_src_tgt_weight_sharing=model_opt.embs_share_weight,
            d_k=model_opt.d_k,
            d_v=model_opt.d_v,
            d_model=model_opt.d_model,
            d_word_vec=model_opt.d_word_vec,
            d_inner=model_opt.d_inner_hid,
            n_layers=model_opt.n_layers,
            n_head=model_opt.n_head,
            dropout=model_opt.dropout,
            positional=model_opt.positional)

        model.load_state_dict(checkpoint['model'])
        print('[Info] Trained model state loaded.')

        model.word_prob_prj = nn.LogSoftmax(dim=1)

        model = model.to(self.device)

        self.model = model
        self.model.eval()

    def translate_batch(self, src_seq, src_pos, src_sen_pos):
        ''' Translation work in one batch '''

        def get_inst_idx_to_tensor_position_map(inst_idx_list):
            ''' Indicate the position of an instance in a tensor. '''
            return {inst_idx: tensor_position for tensor_position, inst_idx in enumerate(inst_idx_list)}

        def collect_active_part(beamed_tensor, curr_active_inst_idx, n_prev_active_inst, n_bm):
            ''' Collect tensor parts associated to active instances. '''

            _, *d_hs = beamed_tensor.size()
            n_curr_active_inst = len(curr_active_inst_idx)
            new_shape = (n_curr_active_inst * n_bm, *d_hs)

            beamed_tensor = beamed_tensor.view(n_prev_active_inst, -1)
            beamed_tensor = beamed_tensor.index_select(0, curr_active_inst_idx)
            beamed_tensor = beamed_tensor.view(*new_shape)

            return beamed_tensor

        def collate_active_info(
                src_seq, src_enc, inst_idx_to_position_map, active_inst_idx_list):
            # Sentences which are still active are collected,
            # so the decoder will not run on completed sentences.
            n_prev_active_inst = len(inst_idx_to_position_map)
            active_inst_idx = [inst_idx_to_position_map[k] for k in active_inst_idx_list]
            active_inst_idx = torch.LongTensor(active_inst_idx).to(self.device)

            active_src_seq = collect_active_part(src_seq, active_inst_idx, n_prev_active_inst, n_bm)
            active_src_enc = collect_active_part(src_enc, active_inst_idx, n_prev_active_inst, n_bm)
            active_inst_idx_to_position_map = get_inst_idx_to_tensor_position_map(active_inst_idx_list)

            return active_src_seq, active_src_enc, active_inst_idx_to_position_map

        def beam_decode_step(
                inst_dec_beams, len_dec_seq, src_seq, enc_output, inst_idx_to_position_map, n_bm):
            ''' Decode and update beam status, and then return active beam idx '''

            def prepare_beam_dec_seq(inst_dec_beams, len_dec_seq):
                dec_partial_seq = [b.get_current_seq_state() for b in inst_dec_beams if not b.done]
                dec_partial_seq = torch.stack(dec_partial_seq).to(self.device)
                dec_partial_seq = dec_partial_seq.view(-1, len_dec_seq)
                return dec_partial_seq

            def prepare_beam_dec_pos(inst_dec_beams, len_dec_seq):
                dec_partial_seq = [b.get_current_pos_state() for b in inst_dec_beams if not b.done]
                dec_partial_seq = torch.stack(dec_partial_seq).to(self.device)
                dec_partial_seq = dec_partial_seq.view(-1, len_dec_seq)
                return dec_partial_seq

            def prepare_beam_dec_sen_pos(inst_dec_beams, len_dec_seq):
                #print('inst_dec_beams',inst_dec_beams)
                #print('inst_dec_beams', type(inst_dec_beams))
                #print('inst_dec_beams',inst_dec_beams.size)
                #print('inst_dec_beams[0]', inst_dec_beams[0].get_current_sen_pos_state())
                dec_partial_seq = [b.get_current_sen_pos_state() for b in inst_dec_beams if not b.done]
                #print('dec_partial_seq',dec_partial_seq)
                dec_partial_seq = torch.stack(dec_partial_seq).to(self.device)
                dec_partial_seq = dec_partial_seq.view(-1, len_dec_seq)
                return dec_partial_seq

            def predict_word(dec_seq, dec_pos, dec_sen_pos, src_seq, enc_output, n_active_inst, n_bm):
                #print('dec_seq:',dec_seq.shape)
                #print('dec_pos:', dec_pos.shape)
                #print('dec_sen_pos:', dec_sen_pos.shape)
                #print('src_seq:', src_seq.shape)
                #print('enc_output', enc_output.shape)
                #print('n_active_inst', n_active_inst)
                #print('n_bm:', n_bm)

                
                dec_output, *_ = self.model.decoder(dec_seq, dec_pos, dec_sen_pos, src_seq, enc_output)
                dec_output = dec_output[:, -1, :]  # Pick the last step: (bh * bm) * d_h
                logits = self.model.tgt_word_prj(dec_output)
                ## word_prob actually
                logits = F.log_softmax(logits, dim=1)
                # UNK mask
                logits[:, Constants.UNK] = -1e19
                #rm_set = set(Constants.BOSs+[13])
                rm_set = set(Constants.BOSs+[19]) # 19 => "."
                #logits[:, 19] -= logits[:, 19].abs()* + 1e-10
                for i, (ins, pos, sen_pos) in enumerate(zip(dec_seq, dec_pos, dec_sen_pos)):
                    current_sen_pos = sen_pos[-1]
                    for token, s_pos in zip(ins.flip(0), sen_pos.flip(0)):
                        length_norm = len(ins)
                        if token.item() not in rm_set and s_pos == current_sen_pos:
                            logits[i, token] -= 5+1e-19
                        if s_pos != current_sen_pos:
                            logits[i, token] -= 20/length_norm - 1e-19
                            #break

                #word_prob = F.log_softmax(logits, dim=1)
                #word_prob = word_prob.view(n_active_inst, n_bm, -1)
                word_prob = logits.view(n_active_inst, n_bm, -1)

                return word_prob

            def collect_active_inst_idx_list(inst_beams, word_prob, inst_idx_to_position_map):
                active_inst_idx_list = []
                for inst_idx, inst_position in inst_idx_to_position_map.items():
                    is_inst_complete = inst_beams[inst_idx].advance(word_prob[inst_position])
                    if not is_inst_complete:
                        active_inst_idx_list += [inst_idx]

                return active_inst_idx_list

            n_active_inst = len(inst_idx_to_position_map)

            dec_seq = prepare_beam_dec_seq(inst_dec_beams, len_dec_seq)
            dec_pos = prepare_beam_dec_pos(inst_dec_beams, len_dec_seq)
            dec_sen_pos = prepare_beam_dec_sen_pos(inst_dec_beams, len_dec_seq)
            #dec_pos = prepare_beam_dec_pos(len_dec_seq, n_active_inst, n_bm)
            #dec_pos, dec_sen_pos = prepare_beam_dec_pos(dec_seq)
            word_prob = predict_word(dec_seq, dec_pos, dec_sen_pos, src_seq, enc_output, n_active_inst, n_bm)

            # Update the beam with predicted word prob information and collect incomplete instances
            active_inst_idx_list = collect_active_inst_idx_list(
                inst_dec_beams, word_prob, inst_idx_to_position_map)

            return active_inst_idx_list

        def collect_hypothesis_and_scores(inst_dec_beams, n_best):
            all_hyp, all_scores = [], []
            for inst_idx in range(len(inst_dec_beams)):
                scores, tail_idxs = inst_dec_beams[inst_idx].sort_scores()
                all_scores += [scores[:n_best]]

                hyps = [inst_dec_beams[inst_idx].get_seq_hypothesis(i) for i in tail_idxs[:n_best]]
                all_hyp += [hyps]
            return all_hyp, all_scores

        with torch.no_grad():
            #-- Encode
            src_seq, src_pos, src_sen_pos = src_seq.to(self.device), src_pos.to(self.device), src_sen_pos.to(self.device)
            src_enc, *_ = self.model.encoder(src_seq, src_pos, src_sen_pos)

            #-- Repeat data for beam search
            n_bm = self.opt.beam_size
            n_inst, len_s, d_h = src_enc.size()

            #print('n_inst',n_inst)
            #print('len_s',len_s)
            #print('d_h',d_h)
            #print('n_bm',n_bm)

            src_seq = src_seq.repeat(1, n_bm).view(n_inst * n_bm, len_s)
            src_enc = src_enc.repeat(1, n_bm, 1).view(n_inst * n_bm, len_s, d_h)

            #-- Prepare beams
            inst_dec_beams = [Beam(n_bm, device=self.device) for _ in range(n_inst)]

            #print('inst_dec_beams.shape',len(inst_dec_beams))
            #print('inst_dec_beams.shape',inst_dec_beams[0].size)
            #print('inst_dec_beams[0].getcurrent', inst_dec_beams[0].get_current_sen_pos_state())
            #-- Bookkeeping for active or not
            active_inst_idx_list = list(range(n_inst))
            inst_idx_to_position_map = get_inst_idx_to_tensor_position_map(active_inst_idx_list)

            #-- Decode
            #for len_dec_seq in range(1, self.model_opt.max_token_seq_len + 1):
            for len_dec_seq in range(1, 200):
                active_inst_idx_list = beam_decode_step(
                    inst_dec_beams, len_dec_seq, src_seq, src_enc, inst_idx_to_position_map, n_bm)

                if not active_inst_idx_list or len_dec_seq > 50:
                    break  # all instances have finished their path to <EOS>

                src_seq, src_enc, inst_idx_to_position_map = collate_active_info(
                    src_seq, src_enc, inst_idx_to_position_map, active_inst_idx_list)

        batch_hyp, batch_scores = collect_hypothesis_and_scores(inst_dec_beams, self.opt.n_best)

        return batch_hyp, batch_scores
