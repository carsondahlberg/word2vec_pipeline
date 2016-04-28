import collections, itertools, os, ast
import numpy as np
import pandas as pd
import h5py

from gensim.models.word2vec import Word2Vec
from utils.mapreduce import corpus_iterator

import tqdm

class document_scores(corpus_iterator):

    def __init__(self,*args,**kwargs):
        super(document_scores, self).__init__(*args,**kwargs)

        f_w2v = os.path.join(
            kwargs["embedding"]["output_data_directory"],
            kwargs["embedding"]["w2v_embedding"]["f_db"],
        )

         # Load the model from disk
        self.M = Word2Vec.load(f_w2v)
        self.shape = self.M.syn0.shape
        
        # Build total counts
        self.counts = {}
        for key,val in self.M.vocab.items():
            self.counts[key] = val.count

        # Build the dictionary
        self.methods = kwargs["methods"]
        vocab_n = self.shape[0]
        self.word2index = dict(zip(self.M.index2word,range(vocab_n)))

        # Set parallel option
        self._PARALLEL = kwargs["_PARALLEL"]

    def score_document(self, item):

        text = item[0]
        idx  = item[1]
        meta = item[2]
        other_args = item[3:]
        
        tokens = text.split()

        # Find out which tokens are defined
        valid_tokens = [w for w in tokens if w in self.M]
        local_counts = collections.Counter(valid_tokens)
        tokens = set(valid_tokens)
        method = self.current_method

        dim = self.M.syn0.shape[1]

        no_token_FLAG = False
        if not tokens:
            msg = "Document has no valid tokens! This is problem."
            #raise ValueError(msg)
            no_token_FLAG = True

        # If scoring function requires meta, convert it
        if method in ["pos_split"]:
            meta = ast.literal_eval(meta)

        # Lookup the weights (model dependent)
        if method in ["unique","pos_split"]:
            weights = dict.fromkeys(tokens, 1.0)
        elif method in ["simple","svd_stack"]:
            weights = dict([(w,local_counts[w]) for w in tokens])
        elif method in ["TF_IDF","kSVD"]:
            weights = dict([(w,IDF[w]*c) 
                            for w,c in local_counts.items()])
        else:
            msg = "UNKNOWN w2v method {}".format(method)
            raise KeyError(msg)

        # Lookup the embedding vector
        if method in ["unique","simple","TF_IDF","svd_stack"]:
            DV = np.array([self.M[w] for w in tokens])
        elif method in ["kSVD"]:
            word_idx = [self.word2index[w] for w in tokens]
            DV = [self.kSVD_gamma[n] for n in word_idx]
        elif method in ["pos_split"]:

            known_tags = ["N","ADJ","V"]
            dim = self.M.syn0.shape[1]
            pos_vecs = {}
            pos_totals = {}
            for pos in known_tags:
                pos_vecs[pos] = np.zeros((dim,),dtype=float)
                pos_totals[pos] = 0

            POS = meta["POS"]
            ordered_tokens = [t for t in text.split()]
            for token,pos in zip(text.split(),meta["POS"]):
                if token in valid_tokens and pos in known_tags:

                    # This is the "unique" weights
                    if token in pos_vecs:
                        continue
                    pos_vecs[pos]   += self.M[token]
                    pos_totals[pos] += 1

            # Normalize
            for pos in known_tags:
                pos_vecs[pos] /= pos_totals[pos]
            
        else:
            msg = "UNKNOWN w2v method '{}'".format(method)
            raise KeyError(msg)


        # Sum all the vectors with their weights
        if method in ["simple","unique"]:
            # Build the weight matrix
            W  = np.array([weights[w] for w in tokens]).reshape(-1,1)
            DV = np.array(DV)

            doc_vec = (W*DV).sum(axis=0)

            # Renormalize onto the hypersphere
            doc_vec /= np.linalg.norm(doc_vec)

            # Sanity check, L1 norm
            if not no_token_FLAG:
                assert(np.isclose(1.0, np.linalg.norm(doc_vec)))
            else:
                doc_vec = np.zeros(dim,dtype=float)
                
        elif method in ["pos_split"]:
            
            # Concatenate
            doc_vec = np.hstack([pos_vecs[pos] for pos in known_tags])

            # Set any missing pos to zero
            if np.isnan(doc_vec).any():
                bad_idx = np.isnan(doc_vec)
                doc_vec[bad_idx] = 0.0
            
            if no_token_FLAG:
                doc_vec = np.zeros(dim*len(known_tags),dtype=float)

        elif method in ["svd_stack"]:
            # Build the weight matrix
            W  = np.array([weights[w] for w in tokens]).reshape(-1,1)
            DV = np.array(DV)

            n = 2
            _U,_s,_V = np.linalg.svd(DV)
            doc_vec = np.hstack([np.hstack(_V[:n]), _s[:n]])

            if no_token_FLAG:
                doc_vec = np.zeros(dim*n,dtype=float)
            
        else:
            msg = "UNKNOWN w2v method '{}'".format(method)
            raise KeyError(msg)

        
        # Sanity check
        assert(not np.isnan(doc_vec).any()) 

        return [doc_vec,idx,] + other_args

    def compute(self, config):
        '''
        if self._PARALLEL:
            import multiprocessing
            MP = multiprocessing.Pool()
            ITR = MP.imap(self.score_document, self.iter_func())
        else:
            ITR = itertools.imap(self.score_document, self.iter_func())
        '''

        for self.current_method in self.methods:
            print "Scoring {}".format(self.current_method)
            ITR = itertools.imap(self.score_document, self)
            
            data = []
            for result in tqdm.tqdm(ITR):
                data.append(result)

            df = pd.DataFrame(data=data,
                              columns=["V","idx","table_name","f_sql"])

            # Fold over the table_names
            data = []
            for tag,rows in df.groupby(["idx","f_sql"]):
                idx, f_sql = tag
                
                item = {
                    "idx"  :idx,
                    "f_sql":f_sql,
                    "V":np.hstack(rows.V.values),
                }
                data.append(item)
                
            df = pd.DataFrame.from_dict(data)

            self.save(config, df)

    def save(self, config, df):

        method = self.current_method

        print "Saving the scored documents"
        out_dir = config["output_data_directory"]
        f_db = os.path.join(out_dir, config["document_scores"]["f_db"])

        # Create the h5 file if it doesn't exist
        if not os.path.exists(f_db):
            h5 = h5py.File(f_db,'w')
        else:
            h5 = h5py.File(f_db,'r+')

        for key,data_group in df.groupby("f_sql"):

            # Save into the group of the base file name
            name = '.'.join(os.path.basename(key).split('.')[:-1])
            
            g  = h5.require_group(method)

            V = np.array(data_group["V"].tolist())
            
            print "Saving", name, method, V.shape

            all_sizes = set([x.shape for x in V])
            if len(all_sizes) != 1:
                msg = "Method {} failed, sizes differ {}"
                raise ValueError(msg.format(name, all_sizes))

            
            if name in g:
                del g[name]

            g.create_dataset(name,
                             data=V,
                             compression='gzip')

        h5.close()