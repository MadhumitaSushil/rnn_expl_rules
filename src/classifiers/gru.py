from src.torch_utils import EmbeddingMul
from src.torch_utils import TorchUtils
from src.explanations.grads import Explanation
from src.explanations.eval import InterpretabilityEval

import torch.nn as nn
import torch
import torch.nn.functional as F

from random import shuffle

class GRUClassifier(nn.Module):

    def __init__(self, n_layers, hidden_dim, vocab_size, padding_idx, embedding_dim, dropout, label_size, batch_size):

        super(GRUClassifier, self).__init__()

        self.n_gru_layers = n_layers
        self.hidden_dim = hidden_dim
        self.vocab_size = vocab_size
        self.emb_dim = embedding_dim
        self.dropout = dropout
        self.batch_size = batch_size
        self.n_labels = label_size

        if torch.cuda.is_available():
            self.device = torch.device('cuda:1')
        else:
            self.device = torch.device('cpu')

        self.hidden_in = self.init_hidden()  # initialize cell states

        # self.word_embeddings = nn.Embedding(self.vocab_size, self.emb_dim, padding_idx = padding_idx).to(self.device) #embedding layer, initialized at random
        self.word_embeddings = EmbeddingMul(self.vocab_size, self.emb_dim, padding_idx=padding_idx) #embedding layer, initialized at random

        self.gru = nn.GRU(self.emb_dim, self.hidden_dim, num_layers=self.n_gru_layers, dropout=self.dropout) #gru layers

        self.hidden2label = nn.Linear(self.hidden_dim, self.n_labels) #hidden to output layer

        self.to(self.device)

    def init_hidden(self):
        '''
        initializes hidden and cell states to zero for the first input
        '''
        h0 = torch.zeros(self.n_gru_layers, self.batch_size, self.hidden_dim).to(self.device)
        return h0

    def detach_hidden_(self):
        self.hidden_in.detach_()

    def forward(self, sentence, sent_lengths, hidden):

        sort, unsort = TorchUtils.get_sort_unsort(sent_lengths)

        embs = self.word_embeddings(sentence).to(self.device) # word sequence to embedding sequence

        # truncating the batch length if last batch has fewer elements
        cur_batch_len = len(sent_lengths)
        hidden = hidden[:, :cur_batch_len, :]

        # view reshapes the data to the given dimensions. -1: infer from the rest. We want (seq_len * batch_size * input_size)
        # embs = embeds.view(sentence.shape[0], sentence.shape[1], -1)

        # converts data to packed sequences with data and batch size at every time step after sorting them per lengths
        embs = nn.utils.rnn.pack_padded_sequence(embs[:, sort], sent_lengths[sort], batch_first=False)

        # gru_out: output of last gru layer after every time step
        # self.hidden gets the updated hidden and cell states at the end of the sequence
        gru_out, hidden = self.gru(embs, hidden)
        # pad the sequences again to convert to original padded data shape
        gru_out, lengths = nn.utils.rnn.pad_packed_sequence(gru_out, batch_first=False)
        # embs, __ = nn.utils.rnn.pad_packed_sequence(embs, batch_first=False)

        #unsort batch
        gru_out = gru_out[:, unsort]
        hidden = hidden[:, unsort, :]
        # use the output of the last GRU layer at the end of the last valid timestep to predict output
        # If sequence len is constant, using hidden is the same as gru_out[-1].
        # For variable len seq, use hidden for the hidden state at last valid timestep. Do it for the last hidden layer
        y = self.hidden2label(hidden[-1])
        y = F.log_softmax(y, dim=1)

        return y

    def loss(self, fwd_out, target):
        #NLL loss to be used when logits have log-softmax output.
        #If softmax layer is not added, directly CrossEntropyLoss can be used.
        loss_fn = nn.NLLLoss()
        return loss_fn(fwd_out, target)

    def train_model(self, corpus, corpus_encoder, n_epochs, optimizer):

        self.train()

        optimizer = optimizer

        for i in range(n_epochs):
            running_loss = 0.0

            #shuffle the corpus
            combined = list(zip(corpus.fname_subset, corpus.labels))
            shuffle(combined)
            corpus.fname_subset, corpus.labels = zip(*combined)

            #get train batch
            for idx, (cur_insts, cur_labels) in enumerate(corpus_encoder.get_batches(corpus, self.batch_size)):
                cur_insts, cur_labels, cur_lengths = corpus_encoder.batch_to_tensors(cur_insts, cur_labels, self.device)

                # forward pass
                fwd_out = self.forward(cur_insts, cur_lengths, self.hidden_in)

                # loss calculation
                loss = self.loss(fwd_out, cur_labels)

                # backprop
                optimizer.zero_grad()  # reset tensor gradients
                loss.backward()  # compute gradients for network params w.r.t loss
                optimizer.step()  # perform the gradient update step

                #detach hidden nodes from the graph. IMP to prevent the graph from growing.
                self.detach_hidden_()

                # print statistics
                running_loss += loss.item()

                if idx % 100 == 99:  # print every 100 mini-batches
                    print('[%d, %5d] loss: %.3f' %
                          (i + 1, idx + 1, running_loss / 100))
                    running_loss = 0.0

    def predict(self, corpus, corpus_encoder):

        self.eval()

        y_pred = list()
        y_true = list()

        for idx, (cur_insts, cur_labels) in enumerate(corpus_encoder.get_batches(corpus, self.batch_size)):
            cur_insts, cur_labels, cur_lengths = corpus_encoder.batch_to_tensors(cur_insts, cur_labels, self.device)

            y_true.extend(cur_labels.cpu().numpy())

            self.detach_hidden_()

            # forward pass
            fwd_out = self.forward(cur_insts, cur_lengths, self.hidden_in)

            __, cur_preds = torch.max(fwd_out.detach(), 1)  # first return value is the max value, second is argmax
            y_pred.extend(cur_preds.cpu().numpy())


        return y_pred, y_true

    def save(self, f_model = 'gru_classifier.tar', dir_model = '../out/'):

        net_params = {'n_layers': self.n_gru_layers,
                      'hidden_dim': self.hidden_dim,
                      'vocab_size': self.vocab_size,
                      'padding_idx': self.word_embeddings.padding_idx,
                      'embedding_dim': self.emb_dim,
                      'dropout': self.dropout,
                      'label_size': self.n_labels,
                      'batch_size': self.batch_size
                      }

        # save model state
        state = {
            'net_params': net_params,
            'state_dict': self.state_dict(),
        }

        TorchUtils.save_model(state, f_model, dir_model)

    @classmethod
    def load(cls, f_model = 'gru_classifier.tar', dir_model = '../out/'):

        state = TorchUtils.load_model(f_model, dir_model)
        classifier = cls(**state['net_params'])
        classifier.load_state_dict(state['state_dict'])

        return classifier

    def _set_eval(self):
        '''
        Switch off the training behaviour of the parameters
        '''
        for p in self.parameters():
            p.train = False

    def get_importance(self, corpus, corpus_encoder):
        '''
        Compute word importance scores based on backpropagated gradients
        '''
        explanation = Explanation.get_grad_importance(self, corpus, corpus_encoder, 'mod_dot', 'gru')
        eval_obj = InterpretabilityEval()

        eval_obj.avg_prec_recall_f1_at_k_from_corpus(explanation.imp_scores, corpus, corpus_encoder, k = 15)


if __name__ == '__main__':
    gru = GRUClassifier(2, 100, 50, 50, 0.5, 2, 2)

    #variable length sequences
    X0 = torch.LongTensor([1, 5, 8, 19, 43])
    X1 = torch.LongTensor([23,44,5,13,1,34,43])
    X = [X1, X0] #sequence needs to be sorted in descending order of length

    X_padded = nn.utils.rnn.pad_sequence(X, batch_first=True)
    # print(X_padded)

    #In case of error, change line in forward: embs = embeds.view(sentence.shape[1], sentence.shape[0], -1)
    fwd_out = gru.forward(X_padded, [7, 5], gru.hidden_in)

    labels = torch.LongTensor([[1,0],[0,1]])

    loss = gru.loss(fwd_out, labels)
    print(loss)