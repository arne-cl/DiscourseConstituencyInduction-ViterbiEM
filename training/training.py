import jsonlines
import numpy as np
import chainer
import chainer.functions as F
from chainer import cuda, serializers
import multiset

import utils
import treetk

import treesamplers
import parsing
import rst_parseval
import old_rst_parseval

def train(model,
          decoder,
          sampler,
          max_epoch,
          n_init_epochs,
          negative_size,
          batch_size,
          weight_decay,
          gradient_clipping,
          optimizer_name,
          train_databatch,
          dev_databatch,
          path_train,
          path_valid,
          path_snapshot,
          path_pred,
          path_gold):
    """
    :type model: Model
    :type decoder: IncrementalCKYDecoder
    :type sampler: TreeSampler
    :type max_epoch: int
    :type n_init_epochs: int
    :type negative_size: int
    :type batch_size: int
    :type weight_decay: float
    :type gradient_clipping: float
    :type optimizer_name: str
    :type train_databatch: DataBatch
    :type dev_databatch: DataBatch
    :type path_train: str
    :type path_valid: str
    :type path_snapshot: str
    :type path_pred: str
    :type path_gold: str
    :rtype: None
    """
    writer_train = jsonlines.Writer(open(path_train, "w"), flush=True)
    if dev_databatch is not None:
        writer_valid = jsonlines.Writer(open(path_valid, "w"), flush=True)

    boundary_flags = [(True,False)]
    assert negative_size >= len(boundary_flags)
    negative_tree_sampler = treesamplers.NegativeTreeSampler()

    # Optimizer preparation
    opt = utils.get_optimizer(optimizer_name)
    opt.setup(model)
    if weight_decay > 0.0:
        opt.add_hook(chainer.optimizer.WeightDecay(weight_decay))
    if gradient_clipping:
        opt.add_hook(chainer.optimizer.GradientClipping(gradient_clipping))

    n_train = len(train_databatch)
    it = 0
    bestscore_holder = utils.BestScoreHolder(scale=100.0)
    bestscore_holder.init()

    if dev_databatch is not None:
        # Initial validation
        with chainer.using_config("train", False), chainer.no_backprop_mode():
            parsing.parse(model=model,
                          decoder=decoder,
                          databatch=dev_databatch,
                          path_pred=path_pred)
            scores = rst_parseval.evaluate(
                        pred_path=path_pred,
                        gold_path=path_gold)
            old_scores = old_rst_parseval.evaluate(
                        pred_path=path_pred,
                        gold_path=path_gold)
            out = {
                    "epoch": 0,
                    "Morey2018": {
                        "Unlabeled Precision": scores["S"]["Precision"] * 100.0,
                        "Precision_info": scores["S"]["Precision_info"],
                        "Unlabeled Recall": scores["S"]["Recall"] * 100.0,
                        "Recall_info": scores["S"]["Recall_info"],
                        "Micro F1": scores["S"]["Micro F1"] * 100.0},
                    "Marcu2000": {
                        "Unlabeled Precision": old_scores["S"]["Precision"] * 100.0,
                        "Precision_info": old_scores["S"]["Precision_info"],
                        "Unlabeled Recall": old_scores["S"]["Recall"] * 100.0,
                        "Recall_info": old_scores["S"]["Recall_info"],
                        "Micro F1": old_scores["S"]["Micro F1"] * 100.0}}
            writer_valid.write(out)
            utils.writelog(utils.pretty_format_dict(out))
        # Saving
        bestscore_holder.compare_scores(scores["S"]["Micro F1"], step=0)
        serializers.save_npz(path_snapshot, model)
        utils.writelog("Saved the model to %s" % path_snapshot)
    else:
        # Saving
        serializers.save_npz(path_snapshot, model)
        utils.writelog("Saved the model to %s" % path_snapshot)

    for epoch in range(1, max_epoch+1):
        perm = np.random.permutation(n_train)
        for inst_i in range(0, n_train, batch_size):
            # Processing one mini-batch

            # Init
            loss_constituency, acc_constituency = 0.0, 0.0
            actual_batchsize = 0

            # Mini-batch preparation
            batch_edu_ids = train_databatch.batch_edu_ids[perm[inst_i:inst_i+batch_size]]
            batch_edus = train_databatch.batch_edus[perm[inst_i:inst_i+batch_size]]
            batch_edus_postag = train_databatch.batch_edus_postag[perm[inst_i:inst_i+batch_size]]
            batch_edus_head = train_databatch.batch_edus_head[perm[inst_i:inst_i+batch_size]]
            batch_sbnds = train_databatch.batch_sbnds[perm[inst_i:inst_i+batch_size]]
            batch_pbnds = train_databatch.batch_pbnds[perm[inst_i:inst_i+batch_size]]
            # batch_bin_sexp = train_databatch.batch_bin_sexp[perm[inst_i:inst_i+batch_size]] # NOTE: Oracle

            # for edu_ids, edus, edus_postag, edus_head, sbnds, pbnds, gold_bin_sexp \
            #         in zip(batch_edu_ids,
            #                batch_edus,
            #                batch_edus_postag,
            #                batch_edus_head,
            #                batch_sbnds,
            #                batch_pbnds,
            #                batch_bin_sexp): # NOTE: Oracle
            for edu_ids, edus, edus_postag, edus_head, sbnds, pbnds \
                    in zip(batch_edu_ids,
                           batch_edus,
                           batch_edus_postag,
                           batch_edus_head,
                           batch_sbnds,
                           batch_pbnds):
                # Processing one instance

                # Feature extraction
                edu_vectors = model.forward_edus(edus, edus_postag, edus_head) # (n_edus, bilstm_dim)
                padded_edu_vectors = model.pad_edu_vectors(edu_vectors) # (n_edus+2, bilstm_dim)
                mask_bwd, mask_fwd = model.make_masks() # (1, bilstm_dim), (1, bilstm_dim)

                ########## E-Step (BEGIN) ##########
                # Positive tree
                with chainer.using_config("train", False), chainer.no_backprop_mode():

                    # # NOTE: Oracle
                    # gold_bin_tree = treetk.rstdt.postprocess(treetk.sexp2tree(gold_bin_sexp, with_nonterminal_labels=True, with_terminal_labels=False))
                    # gold_bin_tree.calc_spans()
                    # gold_spans = treetk.aggregate_spans(gold_bin_tree, include_terminal=False, order="pre-order") # list of (int, int, str)
                    # pos_spans = [(b,e) for (b,e,l) in gold_spans] # list of (int, int)

                    if epoch <= n_init_epochs:
                        pos_sexp = sampler.sample(
                                        sexps=edu_ids,
                                        edus=edus,
                                        edus_head=edus_head,
                                        sbnds=sbnds,
                                        pbnds=pbnds)
                    else:
                        pos_sexp = decoder.decode(
                                        model=model,
                                        sexps=edu_ids,
                                        edus=edus,
                                        edus_postag=edus_postag,
                                        sbnds=sbnds,
                                        pbnds=pbnds,
                                        padded_edu_vectors=padded_edu_vectors,
                                        mask_bwd=mask_bwd,
                                        mask_fwd=mask_fwd,
                                        use_sbnds=True,
                                        use_pbnds=True)
                    pos_tree = treetk.sexp2tree(pos_sexp, with_nonterminal_labels=False, with_terminal_labels=False)
                    pos_tree.calc_spans()
                    pos_spans = treetk.aggregate_spans(pos_tree, include_terminal=False, order="post-order") # list of (int, int)
                ########## E-Step (END) ##########

                ########## M-Step-1 (BEGIN) ##########
                # Negative tree
                pos_neg_spans = []
                margins = []
                pos_neg_spans.append(pos_spans)
                with chainer.using_config("train", False), chainer.no_backprop_mode():
                    for use_sbnds,use_pbnds in boundary_flags:
                        neg_bin_sexp = decoder.decode(
                                            model=model,
                                            sexps=edu_ids,
                                            edus=edus,
                                            edus_postag=edus_postag,
                                            sbnds=sbnds,
                                            pbnds=pbnds,
                                            padded_edu_vectors=padded_edu_vectors,
                                            mask_bwd=mask_bwd,
                                            mask_fwd=mask_fwd,
                                            use_sbnds=use_sbnds,
                                            use_pbnds=use_pbnds,
                                            gold_spans=pos_spans) # list of str
                        neg_tree = treetk.sexp2tree(neg_bin_sexp, with_nonterminal_labels=False, with_terminal_labels=False)
                        neg_tree.calc_spans()
                        neg_spans = treetk.aggregate_spans(neg_tree, include_terminal=False, order="pre-order") # list of (int, int)
                        margin = compute_tree_distance(pos_spans, neg_spans, coef=1.0)
                        pos_neg_spans.append(neg_spans)
                        margins.append(margin)
                for _ in range(negative_size - len(boundary_flags)):
                    neg_bin_sexp = negative_tree_sampler.sample(sexps=edu_ids, sbnds=sbnds, pbnds=pbnds)
                    neg_tree = treetk.sexp2tree(neg_bin_sexp, with_nonterminal_labels=False, with_terminal_labels=False)
                    neg_tree.calc_spans()
                    neg_spans = treetk.aggregate_spans(neg_tree, include_terminal=False, order="pre-order") # list of (int, int)
                    margin = compute_tree_distance(pos_spans, neg_spans, coef=1.0)
                    pos_neg_spans.append(neg_spans)
                    margins.append(margin)

                # Scoring
                pred_scores = model.forward_spans_for_bracketing(
                                        edus=edus,
                                        edus_postag=edus_postag,
                                        sbnds=sbnds,
                                        pbnds=pbnds,
                                        padded_edu_vectors=padded_edu_vectors,
                                        mask_bwd=mask_bwd,
                                        mask_fwd=mask_fwd,
                                        batch_spans=pos_neg_spans,
                                        aggregate=True) # (1+negative_size, 1)

                # Constituency Loss
                for neg_i in range(negative_size):
                    loss_constituency += F.clip(pred_scores[1+neg_i] + margins[neg_i] - pred_scores[0], 0.0, 10000000.0)
                ########## M-Step-1 (END) ##########

                # Ranked Accuracy
                pred_scores = F.reshape(pred_scores, (1, 1+negative_size)) # (1, 1+negative_size)
                gold_scores = np.zeros((1,), dtype=np.int32) # (1,)
                gold_scores = utils.convert_ndarray_to_variable(gold_scores, seq=False) # (1,)
                acc_constituency += F.accuracy(pred_scores, gold_scores)

                actual_batchsize += 1

            ########## M-Step-2 (BEGIN) ##########
            # Backward & Update
            actual_batchsize = float(actual_batchsize)
            loss_constituency = loss_constituency / actual_batchsize
            acc_constituency = acc_constituency / actual_batchsize
            loss = loss_constituency
            model.zerograds()
            loss.backward()
            opt.update()
            it += 1
            ########## M-Step-2 (END) ##########

            # Write log
            loss_constituency_data = float(cuda.to_cpu(loss_constituency.data))
            acc_constituency_data = float(cuda.to_cpu(acc_constituency.data))
            out = {"iter": it,
                   "epoch": epoch,
                   "progress": "%d/%d" % (inst_i+actual_batchsize, n_train),
                   "progress_ratio": float(inst_i+actual_batchsize)/n_train*100.0,
                   "Constituency Loss": loss_constituency_data,
                   "Ranked Accuracy": acc_constituency_data * 100.0}
            writer_train.write(out)
            utils.writelog(utils.pretty_format_dict(out))
            print(bestscore_holder.best_score * 100.0)

        if dev_databatch is not None:
            # Validation
            with chainer.using_config("train", False), chainer.no_backprop_mode():
                parsing.parse(model=model,
                              decoder=decoder,
                              databatch=dev_databatch,
                              path_pred=path_pred)
                scores = rst_parseval.evaluate(
                            pred_path=path_pred,
                            gold_path=path_gold)
                old_scores = old_rst_parseval.evaluate(
                            pred_path=path_pred,
                            gold_path=path_gold)
                out = {
                        "epoch": epoch,
                        "Morey2018": {
                            "Unlabeled Precision": scores["S"]["Precision"] * 100.0,
                            "Precision_info": scores["S"]["Precision_info"],
                            "Unlabeled Recall": scores["S"]["Recall"] * 100.0,
                            "Recall_info": scores["S"]["Recall_info"],
                            "Micro F1": scores["S"]["Micro F1"] * 100.0},
                        "Marcu2000": {
                            "Unlabeled Precision": old_scores["S"]["Precision"] * 100.0,
                            "Precision_info": old_scores["S"]["Precision_info"],
                            "Unlabeled Recall": old_scores["S"]["Recall"] * 100.0,
                            "Recall_info": old_scores["S"]["Recall_info"],
                            "Micro F1": old_scores["S"]["Micro F1"] * 100.0}}
                writer_valid.write(out)
                utils.writelog(utils.pretty_format_dict(out))
            # Saving
            did_update = bestscore_holder.compare_scores(scores["S"]["Micro F1"], epoch)
            if did_update:
                serializers.save_npz(path_snapshot, model)
                utils.writelog("Saved the model to %s" % path_snapshot)
            # Finished?
            if bestscore_holder.ask_finishing(max_patience=10):
                utils.writelog("Patience %d is over. Training finished successfully." % bestscore_holder.patience)
                writer_train.close()
                if dev_databatch is not None:
                    writer_valid.close()
                return
        else:
            # No validation
            # Saving
            serializers.save_npz(path_snapshot, model)
            # We continue training until it reaches the maximum number of epochs.

def compute_tree_distance(spans1, spans2, coef):
    """
    :type spans1: list of (int, int)
    :type spans2: list of (int, int)
    :type coef: float
    :rtype: float
    """
    assert len(spans1) == len(spans2)

    spans1 = multiset.Multiset(spans1)
    spans2 = multiset.Multiset(spans2)

    assert len(spans1) == len(spans2)
    dist = len(spans1) - len(spans1 & spans2)
    dist = float(dist)

    dist = coef * dist
    return dist

