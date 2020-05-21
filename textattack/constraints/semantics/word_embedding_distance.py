import numpy as np
import os
import pickle
import torch

from textattack.shared import utils
from textattack.constraints import Constraint
from textattack.shared import TokenizedText
from textattack.shared.validators import transformation_consists_of_word_swaps

class WordEmbeddingDistance(Constraint):
    """
    todo document here
    
    Args:
        word_embedding (str): The word embedding to use
        embedding_type (str): The word embedding to use
        include_unknown_words (bool): Whether or not C(x,x_adv) is true
            if the embedding of x or x_adv is unknown
        min_cos_sim: the minimum cosine similarity between word embeddings
        max_mse_dist: the maximum euclidean distance between word embeddings
        embedding_cased (bool): whether embedding supports uppercase & lowercase
            (defaults to False, or just lowercase)
    """
    PATH = 'word_embeddings'
    def __init__(self, embedding_type='paragramcf', include_unknown_words=True,
        min_cos_sim=None, max_mse_dist=None, cased=False):
        self.include_unknown_words = include_unknown_words
        self.cased = cased
        self.min_cos_sim = min_cos_sim
        self.max_mse_dist = max_mse_dist
        
        self.embedding_type = embedding_type
        if embedding_type == 'paragramcf':
            word_embeddings_folder = 'paragramcf'
            word_embeddings_file = 'paragram.npy'
            word_list_file = 'wordlist.pickle'
            mse_dist_file = 'mse_dist.p'
            cos_sim_file  = 'cos_sim.p'
        else:
            raise ValueError(f'Could not find word embedding {word_embedding}')

        # Download embeddings if they're not cached.
        word_embeddings_path = utils.download_if_needed(WordEmbeddingDistance.PATH)
        word_embeddings_folder = os.path.join(word_embeddings_path, word_embeddings_folder)
        
        # Concatenate folder names to create full path to files.
        word_embeddings_file = os.path.join(word_embeddings_folder, word_embeddings_file)
        word_list_file = os.path.join(word_embeddings_folder, word_list_file)
        mse_dist_file = os.path.join(word_embeddings_folder, mse_dist_file)
        cos_sim_file = os.path.join(word_embeddings_folder, cos_sim_file)
        
        # Actually load the files from disk.
        self.word_embeddings = np.load(word_embeddings_file)
        self.word_embedding_word2index = np.load(word_list_file, allow_pickle=True)
        # Precomputed distance matrices store distances at mat[x][y], where
        # x and y are word IDs and x < y.
        if self.max_mse_dist is not None and os.path.exists(mse_dist_file):
            with open(mse_dist_file, 'rb') as f:
                self.mse_dist_mat = pickle.load(f)
        else:
            self.mse_dist_mat = {}
        if self.min_cos_sim is not None and os.path.exists(cos_sim_file):
            with open(cos_sim_file, 'rb') as f:
                self.cos_sim_mat = pickle.load(f)
        else:
            self.cos_sim_mat = {}
        
    def get_cos_sim(self, a, b):
        """ Returns the cosine similarity of words with IDs a and b."""
        if isinstance(a, str):
            a = self.word_embedding_word2index[a]
        if isinstance(b, str):
            b = self.word_embedding_word2index[b]
        a, b = min(a, b), max(a,b)
        try:
            cos_sim = self.cos_sim_mat[a][b]
        except KeyError:
            e1 = self.word_embeddings[a]
            e2 = self.word_embeddings[b]
            e1 = torch.tensor(e1).to(utils.get_device())
            e2 = torch.tensor(e2).to(utils.get_device())
            cos_sim = torch.nn.CosineSimilarity(dim=0)(e1, e2)
            self.cos_sim_mat[a][b] = cos_sim
        return cos_sim
    
    def get_mse_dist(self, a, b):
        """ Returns the MSE distance of words with IDs a and b."""
        a, b = min(a, b), max(a,b)
        try:
            mse_dist = self.mse_dist_mat[a][b]
        except KeyError:
            e1 = self.word_embeddings[a]
            e2 = self.word_embeddings[b]
            e1 = torch.tensor(e1).to(utils.get_device())
            e2 = torch.tensor(e2).to(utils.get_device())
            mse_dist = torch.sum((e1 - e2) ** 2)
            self.mse_dist_mat[a][b] = mse_dist
        return mse_dist
    
    def _check_constraint(self, x, x_adv, original_text=None):
        """ Returns true if (x, x_adv) are closer than `self.min_cos_sim`
            and `self.max_mse_dist`. """
        try:
            indices = x_adv.attack_attrs['newly_modified_indices']
        except KeyError:
            raise KeyError('Cannot apply part-of-speech constraint without `newly_modified_indices`')
        
        for i in indices:
            x = x.words[i]
            x_adv = x_adv.words[i]
            
            if not self.cased:
                # If embedding vocabulary is all lowercase, lowercase words.
                x = x.lower()
                x_adv = x_adv.lower()
            
            try:
                x_id = self.word_embedding_word2index[x]
                x_adv_id = self.word_embedding_word2index[x_adv]
            except KeyError:
                # This error is thrown if x or x_adv has no corresponding ID.
                if self.include_unknown_words:
                    continue
                return False
                
            # Check cosine distance.
            if self.min_cos_sim:
                cos_sim = self.get_cos_sim(x_id, x_adv_id)
                if cos_sim < self.min_cos_sim:
                    return False
            # Check MSE distance.
            if self.max_mse_dist:
                mse_dist = self.get_mse_dist(x_id, x_adv_id)
                if mse_dist > self.max_mse_dist:
                    return False

        return True

    def check_compatibility(self, transformation):
        """
        WordEmbeddingDistance requires a word being both deleted and inserted at the same index
        in order to compare their embeddings, therefore it's restricted to word swaps.
        """
        return transformation_consists_of_word_swaps(transformation)
        
    def extra_repr_keys(self):
        """Set the extra representation of the constraint using these keys.
        
        To print customized extra information, you should reimplement
        this method in your own constraint. Both single-line and multi-line
        strings are acceptable.
        """ 
        if self.min_cos_sim is None:
            metric = 'max_mse_dist'
        else:
            metric = 'min_cos_sim'
        return ['embedding_type', metric, 'cased', 'include_unknown_words']
