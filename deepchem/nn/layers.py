"""Custom Keras Layers.
"""
from __future__ import print_function
from __future__ import division
from __future__ import unicode_literals

__author__ = "Han Altae-Tran and Bharath Ramsundar"
__copyright__ = "Copyright 2016, Stanford University"
__license__ = "GPL"


import numpy as np
import tensorflow as tf
from deepchem.nn import activations
from deepchem.nn import initializations
from deepchem.nn import model_ops
from deepchem.nn.copy import Layer
from deepchem.nn.copy import Input
from deepchem.nn.copy import Dense
from deepchem.nn.copy import Dropout

def affine(x, W, b):
  return tf.matmul(x, W) + b

def tf_affine(x, vm, scope):
  W = vm.var(scope, 'W')
  b = vm.var(scope, 'b')

  return tf.matmul(x, W) + b

def sum_neigh(atoms, deg_adj_lists, max_deg):
  """Store the summed atoms by degree"""
  deg_summed = max_deg * [None]

  # Tensorflow correctly processes empty lists when using concat
  for deg in range(1, max_deg+1):
    gathered_atoms = tf.gather(atoms, deg_adj_lists[deg-1])
    # Sum along neighbors as well as self, and store
    summed_atoms = tf.reduce_sum(gathered_atoms, 1)
    deg_summed[deg-1] = summed_atoms
  
  return deg_summed

def graph_conv(atoms, deg_adj_lists, deg_slice, max_deg, min_deg, W_list,
               b_list):
  """Core tensorflow function implementing graph convolution

  Parameters
  ----------
  atoms: tf.Tensor
    Should be of shape (n_atoms, n_feat)
  deg_adj_lists: list
    Of length (max_deg+1-min_deg). The deg-th element is a list of
    adjacency lists for atoms of degree deg.
  deg_slice: tf.Tensor
    Of shape (max_deg+1-min_deg,2). Explained in GraphTopology.
  max_deg: int
    Maximum degree of atoms in molecules.
  min_deg: int
    Minimum degree of atoms in molecules
  W_list: list
    List of learnable weights for convolution.
  b_list: list
    List of learnable biases for convolution.

  Returns
  -------
  tf.Tensor
    Of shape (n_atoms, n_feat)
  """
  W = iter(W_list)
  b = iter(b_list)

  #Sum all neighbors using adjacency matrix
  deg_summed = sum_neigh(atoms, deg_adj_lists, max_deg)

  # Get collection of modified atom features
  new_rel_atoms_collection = (max_deg+1-min_deg) * [None]

  for deg in range(1, max_deg+1):
    # Obtain relevant atoms for this degree
    rel_atoms = deg_summed[deg-1]

    # Get self atoms
    begin = tf.pack([deg_slice[deg-min_deg,0],0])
    size = tf.pack([deg_slice[deg-min_deg,1],-1])
    self_atoms = tf.slice(atoms, begin, size)
    
    # Apply hidden affine to relevant atoms and append
    rel_out = affine(rel_atoms, next(W), next(b))
    self_out = affine(self_atoms, next(W), next(b))
    out = rel_out + self_out
    
    new_rel_atoms_collection[deg-min_deg] = out

  # Determine the min_deg=0 case
  if min_deg == 0:
    deg = 0
    
    begin = tf.pack([deg_slice[deg-min_deg,0],0])
    size = tf.pack([deg_slice[deg-min_deg,1],-1])
    self_atoms = tf.slice(atoms, begin, size)

    # Only use the self layer
    out = affine(self_atoms, next(W), next(b))
    
    new_rel_atoms_collection[deg-min_deg] = out        
      
  # Combine all atoms back into the list
  activated_atoms = tf.concat(0, new_rel_atoms_collection)

  return activated_atoms

def graph_gather(atoms, membership_placeholder, batch_size):
  """
  Parameters
  ----------
  atoms: tf.Tensor
    Of shape (n_atoms, n_feat)
  membership_placeholder: tf.Placeholder
    Of shape (n_atoms,). Molecule each atom belongs to.
  batch_size: int
    Batch size for deep model.

  Returns
  -------
  tf.Tensor
    Of shape (batch_size, n_feat)
  """

  # WARNING: Does not work for Batch Size 1! If batch_size = 1, then use reduce_sum!
  assert (batch_size > 1, "graph_gather requires batches larger than 1")

  # Obtain the partitions for each of the molecules
  activated_par = tf.dynamic_partition(
      atoms, membership_placeholder, batch_size)

  # Sum over atoms for each molecule 
  sparse_reps = [tf.reduce_sum(activated, 0, keep_dims=True)
                 for activated in activated_par]

  # Get the final sparse representations
  sparse_reps = tf.concat(0, sparse_reps)
      
  return sparse_reps

def graph_pool(atoms, deg_adj_lists, deg_slice, max_deg, min_deg):
  """
  Parameters
  ----------
  atoms: tf.Tensor
    Of shape (n_atoms, n_feat)
  deg_adj_lists: list
    Of length (max_deg+1-min_deg). The deg-th element is a list of
    adjacency lists for atoms of degree deg.
  deg_slice: tf.Tensor
    Of shape (max_deg+1-min_deg,2). Explained in GraphTopology.
  max_deg: int
    Maximum degree of atoms in molecules.
  min_deg: int
    Minimum degree of atoms in molecules

  Returns
  -------
  tf.Tensor
    Of shape (batch_size, n_feat)
  """
  # Store the summed atoms by degree
  deg_maxed = (max_deg+1-min_deg) * [None]

  # Tensorflow correctly processes empty lists when using concat

  for deg in range(1, max_deg+1):
    # Get self atoms
    begin = tf.pack([deg_slice[deg-min_deg,0],0])
    size = tf.pack([deg_slice[deg-min_deg,1],-1])
    self_atoms = tf.slice(atoms, begin, size)

    # Expand dims
    self_atoms = tf.expand_dims(self_atoms, 1)
    
    # always deg-1 for deg_adj_lists
    gathered_atoms = tf.gather(atoms, deg_adj_lists[deg-1])
    gathered_atoms = tf.concat(1, [self_atoms, gathered_atoms])
    
    maxed_atoms = tf.reduce_max(gathered_atoms, 1)
    deg_maxed[deg-min_deg] = maxed_atoms

  if min_deg == 0:
    begin = tf.pack([deg_slice[0,0],0])
    size = tf.pack([deg_slice[0,1],-1])
    self_atoms = tf.slice(atoms, begin, size)
    deg_maxed[0] = self_atoms
        
  return tf.concat(0, deg_maxed)

class GraphConv(Layer):
  """"Performs a graph convolution.

  Note this layer expects the presence of placeholders defined by GraphTopology
  and expects that they follow the ordering provided by
  GraphTopology.get_input_placeholders().
  """
  def __init__(self, nb_filter, init='glorot_uniform', activation='linear',
               dropout=None, max_deg=10, min_deg=0, **kwargs):
    """
    Parameters
    ----------
    nb_filter: int
      Number of convolutional filters.
    init: str, optional
      Weight initialization for filters.
    activation: str, optional
      Activation function applied after convolution.
    dropout: float, optional
      Dropout probability.
    max_deg: int, optional
      Maximum degree of atoms in molecules.
    min_deg: int, optional
      Minimum degree of atoms in molecules.
    """
    super(GraphConv, self).__init__(**kwargs)

    self.init = initializations.get(init)  # Set weight initialization
    self.activation = activations.get(activation)  # Get activations
    self.nb_filter = nb_filter  # Save number of filters
    self.dropout = dropout  # Save dropout params
    self.max_deg = max_deg
    self.min_deg = min_deg
    # TODO(rbharath): It's not clear where nb_affine comes from.
    # Is there a solid explanation here?
    self.nb_affine = 2*max_deg + (1-min_deg)        

  def build(self, input_shape):
    """"Construct internal trainable weights.

    This layer expects arguments of form

    [atom_features, deg_slice, membership, deg_adj_list placeholders...]

    input_shape should provide the shapes of each of these tensors.

    Parameters
    ----------
    input_shape: list
      Shapes of incoming tensors
    """
      
    # Generate the nb_affine weights and biases
    atom_features_shape = input_shape[0]
    n_features = atom_features_shape[1]
    self.W_list = [self.init([n_features, self.nb_filter]) 
                   for k in range(self.nb_affine)]
    self.b_list = [model_ops.zeros(shape=[self.nb_filter,])
                   for k in range(self.nb_affine)]

    self.trainable_weights = self.W_list + self.b_list

  def get_output_shape_for(self, input_shape):
    """Output tensor shape produced by this layer."""
    atom_features_shape = input_shape[0]
    assert (len(atom_features_shape) == 2,
            "MolConv only takes 2 dimensional tensors for x")
    n_atoms = atom_features_shape[0]
    return (n_atoms, self.nb_filter)

  def call(self, x, mask=None):
    """Execute this layer on input tensors.

    This layer is meant to be executed on a Graph. So x is expected to
    be a list of placeholders, with the first placeholder the list of
    atom_features (learned or input) at this level, the second the deg_slice,
    the third the membership, and the remaining the deg_adj_lists.

    Visually

    x = [atom_features, deg_slice, membership, deg_adj_list placeholders...]
    
    Parameters
    ----------
    x: list
      list of Tensors of form described above.
    mask: bool, optional
      Ignored. Present only to shadow superclass call() method.

    Returns
    -------
    atom_features: tf.Tensor
      Of shape (n_atoms, nb_filter)
    """
    # Extract atom_features
    atom_features = x[0] 

    # Extract graph topology
    deg_slice, membership, deg_adj_lists = x[1], x[2], x[3:]

    # Perform the mol conv
    atom_features = graph_conv(
        atom_features, deg_adj_lists, deg_slice, self.max_deg,
        self.min_deg, self.W_list, self.b_list)        

    atom_features = self.activation(atom_features)
    
    if self.dropout is not None:
      atom_features = Dropout(self.dropout)(atom_features)

    return atom_features 

class GraphGather(Layer):
  """Gathers information for each molecule.

  The various graph convolution operations expect as input a tensor
  atom_features of shape (n_atoms, n_feat). However, we train on batches of
  molecules at a time. The GraphTopology object groups a list of molecules
  into the atom_features tensor. The tensorial operations are done on this tensor,
  but at the end, the atoms need to be grouped back into molecules. This
  layer takes care of that operation.

  Note this layer expects the presence of placeholders defined by GraphTopology
  and expects that they follow the ordering provided by
  GraphTopology.get_input_placeholders().
  """
  def __init__(self, batch_size, activation='linear', **kwargs):        
    """
    Parameters
    ----------
    batch_size: int
      Number of elements in batch of data.
    """ 
    super(GraphGather, self).__init__(**kwargs)

    self.activation = activations.get(activation)  # Get activations
    self.batch_size = batch_size

  def build(self, input_shape):
    """Nothing needed (no learnable weights)."""
    pass

  def get_output_shape_for(self, input_shape):
    """Output tensor shape produced by this layer."""
    # Extract nodes and membership
    atom_features_shape = input_shape[0]
    membership_shape = input_shape[2]

    assert (len(atom_features_shape) == 2,
            "GraphGather only takes 2 dimensional tensors")
    n_feat = atom_features_shape[1]

    return (self.batch_size, n_feat)

  def call(self, x, mask=None):
    """Execute this layer on input tensors.

    This layer is meant to be executed on a Graph. So x is expected to
    be a list of placeholders, with the first placeholder the list of
    atom_features (learned or input) at this level, the second the deg_slice,
    the third the membership, and the remaining the deg_adj_lists.

    Visually

    x = [atom_features, deg_slice, membership, deg_adj_list placeholders...]
    
    Parameters
    ----------
    x: list
      list of Tensors of form described above.
    mask: bool, optional
      Ignored. Present only to shadow superclass call() method.

    Returns
    -------
    tf.Tensor
      Of shape (batch_size, n_feat), where n_feat is number of atom_features
    """
    # Extract atom_features 
    atom_features = x[0]

    # Extract graph topology
    membership = x[2]

    # Perform the mol gather
    mol_features = graph_gather(atom_features, membership, self.batch_size)

    return self.activation(mol_features)

class GraphPool(Layer):
  """Performs a pooling operation over an arbitrary graph.

  Performs a max pool over the feature vectors for an atom and its neighbors
  in bond-graph. Returns a tensor of the same size as the input.
  """

  def __init__(self, max_deg=10, min_deg=0, **kwargs):        
    """
    Parameters
    ----------
    max_deg: int, optional
      Maximum degree of atoms in molecules.
    min_deg: int, optional
      Minimum degree of atoms in molecules.
    """
    self.max_deg = max_deg
    self.min_deg = min_deg
    super(GraphPool, self).__init__(**kwargs)

  def build(self, input_shape):
    """Nothing needed (no learnable weights)."""
    pass

  def get_output_shape_for(self, input_shape):
    """Output tensor shape produced by this layer."""
    # Extract nodes
    atom_features_shape = input_shape[0]

    assert (len(atom_features_shape) == 2,
            "GraphPool only takes 2 dimensional tensors")
    return atom_features_shape

  def call(self, x, mask=None):
    """Execute this layer on input tensors.

    This layer is meant to be executed on a Graph. So x is expected to
    be a list of placeholders, with the first placeholder the list of
    atom_features (learned or input) at this level, the second the deg_slice,
    the third the membership, and the remaining the deg_adj_lists.

    Visually

    x = [atom_features, deg_slice, membership, deg_adj_list placeholders...]
    
    Parameters
    ----------
    x: list
      list of Tensors of form described above.
    mask: bool, optional
      Ignored. Present only to shadow superclass call() method.

    Returns
    -------
    tf.Tensor
      Of shape (n_atoms, n_feat), where n_feat is number of atom_features
    """
    # Extract atom_features 
    atom_features = x[0]

    # Extract graph topology
    deg_slice, membership, deg_adj_lists = x[1], x[2], x[3:] 

    # Perform the mol gather
    atom_features = graph_pool(
        atom_features, deg_adj_lists, deg_slice, self.max_deg, self.min_deg)

    return atom_features 

class AttnLSTMEmbedding(Layer):
  """Implements AttnLSTM as in matching networks paper.

  References:
  Matching Networks for One Shot Learning
  https://arxiv.org/pdf/1606.04080v1.pdf

  Order Matters: Sequence to sequence for sets
  https://arxiv.org/abs/1511.06391
  """
  def __init__(self, n_test, n_support, max_depth, init='glorot_uniform',
               activation='linear', dropout=None, **kwargs):
    """
    Parameters
    ----------
    n_support: int
      Size of support set.
    n_test: int
      Size of test set.
    max_depth: int
      Number of "processing steps" used by sequence-to-sequence for sets model.
    init: str, optional
      Type of initialization of weights
    activation: str, optional
      Activation for layers.
    dropout: float, optional
      Dropout probability
    """
    super(AttnLSTMEmbedding, self).__init__(**kwargs)

    self.init = initializations.get(init)  # Set weight initialization
    self.activation = activations.get(activation)  # Get activations
    self.max_depth = max_depth
    self.n_test = n_test
    self.n_support = n_support

  def build(self, input_shape):
    """Initializes trainable weights."""
    x_input_shape, xp_input_shape = input_shape  #Unpack

    n_feat = xp_input_shape[1]

    self.lstm = LSTMStep(n_feat)
    self.q_init = model_ops.zeros([self.n_test, n_feat])
    self.r_init = model_ops.zeros([self.n_test, n_feat])
    self.states_init = self.lstm.get_initial_states([self.n_test, n_feat])
    
    self.trainable_weights = [self.q_init, self.r_init]
      
  def get_output_shape_for(self, input_shape):
    """Returns the output shape. Same as input_shape.

    Parameters
    ----------
    input_shape: list
      Will be of form [(n_test, n_feat), (n_support, n_feat)]

    Returns
    -------
    list
      Of same shape as input [(n_test, n_feat), (n_support, n_feat)]
    """
    x_input_shape, xp_input_shape = input_shape  #Unpack

    return input_shape

  def call(self, x_xp, mask=None):
    """Execute this layer on input tensors.

    Parameters
    ----------
    x_xp: list
      List of two tensors (X, Xp). X should be of shape (n_test, n_feat) and
      Xp should be of shape (n_support, n_feat) where n_test is the size of
      the test set, n_support that of the support set, and n_feat is the number
      of per-atom features.

    Returns
    -------
    list
      Returns two tensors of same shape as input. Namely the output shape will
      be [(n_test, n_feat), (n_support, n_feat)]
    """
    # x is test set, xp is support set.
    x, xp = x_xp

    # Get initializations
    q = self.q_init
    #r = self.r_init      
    states = self.states_init
    
    for d in range(self.max_depth):
      # Process using attention
      # Eqn (4), appendix A.1 of Matching Networks paper
      e = cos(x+q, xp)
      a = tf.nn.softmax(e)
      r = model_ops.dot(a, xp)

      # Generate new aattention states
      y = model_ops.concatenate([q, r], axis=1)
      q, states = self.lstm([y] + states) #+ self.lstm.get_constants(x)
                
    return [x+q, xp]

  def compute_mask(self, x, mask=None):
    if not (mask is None):
        return mask
    return [None, None]

class ResiLSTMEmbedding(Layer):
  """Embeds its inputs using an LSTM layer."""
  def __init__(self, n_test, n_support, max_depth, init='glorot_uniform',
               activation='linear', **kwargs):
    """
    Unlike the AttnLSTM model which only modifies the test vectors additively,
    this model allows for an additive update to be performed to both test and
    support using information from each other.

    Parameters
    ----------
    n_support: int
      Size of support set.
    n_test: int
      Size of test set.
    max_depth: int
      Number of LSTM Embedding layers.
    init: string
      Type of weight initialization (from Keras)
    activation: string
      Activation type (ReLu/Linear/etc.)
    """
    super(ResiLSTMEmbedding, self).__init__(**kwargs)

    self.init = initializations.get(init)  # Set weight initialization
    self.activation = activations.get(activation)  # Get activations
    self.max_depth = max_depth
    self.n_test = n_test
    self.n_support = n_support

  def build(self, input_shape):
    """Builds this layer.

    Parameters
    ----------
    input_shape: tuple
      Tuple of ((n_test, n_feat), (n_support, n_feat))
    """
    _, support_input_shape = input_shape  #Unpack
    n_feat = support_input_shape[1]

    # Support set lstm
    self.support_lstm = LSTMStep(n_feat)
    self.q_init = model_ops.zeros([self.n_support, n_feat])
    self.support_states_init = self.support_lstm.get_initial_states(
        [self.n_support, n_feat])

    # Test lstm
    self.test_lstm = LSTMStep(n_feat)
    self.p_init = model_ops.zeros([self.n_test, n_feat])
    self.test_states_init = self.test_lstm.get_initial_states(
        [self.n_test, n_feat])
    
    self.trainable_weights = []
      
  def get_output_shape_for(self, input_shape):
    """Returns the output shape. Same as input_shape.

    Parameters
    ----------
    input_shape: list
      Will be of form [(n_test, n_feat), (n_support, n_feat)]

    Returns
    -------
    list
      Of same shape as input [(n_test, n_feat), (n_support, n_feat)]
    """
    return input_shape

  def call(self, argument, mask=None):
    """Execute this layer on input tensors.

    Parameters
    ----------
    argument: list
      List of two tensors (X, Xp). X should be of shape (n_test, n_feat) and
      Xp should be of shape (n_support, n_feat) where n_test is the size of
      the test set, n_support that of the support set, and n_feat is the number
      of per-atom features.

    Returns
    -------
    list
      Returns two tensors of same shape as input. Namely the output shape will
      be [(n_test, n_feat), (n_support, n_feat)]
    """
    x, xp = argument 

    # Get initializations
    p = self.p_init
    q = self.q_init        
    # Rename support
    z = xp 
    states = self.support_states_init
    x_states = self.test_states_init
    
    for d in range(self.max_depth):
      # Process support xp using attention
      e = cos(z+q, xp)
      a = tf.nn.softmax(e)
      # Get linear combination of support set
      r = model_ops.dot(a, xp)  

      # Not sure if it helps to place the update here or later yet.  Will
      # decide
      #z = r  

      # Process test x using attention
      x_e = cos(x+p, z)
      x_a = tf.nn.softmax(x_e)
      s = model_ops.dot(x_a, z)

      # Generate new support attention states
      qr = model_ops.concatenate([q, r], axis=1)
      q, states = self.support_lstm([qr] + states)

      # Generate new test attention states
      ps = model_ops.concatenate([p, s], axis=1)
      p, x_states = self.test_lstm([ps] + x_states)

      # Redefine  
      z = r  
        
    #return [x+p, z+q]
    return [x+p, xp+q]

  def compute_mask(self, x, mask=None):
    if not (mask is None):
        return mask
    return [None, None]

def cos(x, y):
  denom =  (model_ops.sqrt(
      model_ops.sum(tf.square(x)) * model_ops.sum(tf.square(y)))
      + model_ops.epsilon())
  return model_ops.dot(x, tf.transpose(y)) / denom

class LSTMStep(Layer):
  """ LSTM whose call is a single step in the LSTM.

  This layer exists because the Keras LSTM layer is intrinsically linked to an
  RNN with sequence inputs, and here, we will not be using sequence inputs, but
  rather we generate a sequence of inputs using the intermediate outputs of the
  LSTM, and so will require step by step operation of the lstm
  """
  def __init__(self, output_dim,
               init='glorot_uniform', inner_init='orthogonal',
               forget_bias_init='one', activation='tanh', 
               inner_activation='hard_sigmoid', **kwargs):

    super(LSTMStep, self).__init__(**kwargs)

    self.output_dim = output_dim

    self.init = initializations.get(init)
    self.inner_init = initializations.get(inner_init)
    # No other forget biases supported right now.
    assert forget_bias_init == "one"
    self.forget_bias_init = initializations.get(forget_bias_init)
    self.activation = activations.get(activation)
    self.inner_activation = activations.get(inner_activation)

  def get_initial_states(self, input_shape):
    return [model_ops.zeros(input_shape), model_ops.zeros(input_shape)]

  def build(self, input_shape):
    x, h_tm1, c_tm1 = input_shape # Unpack
    self.input_dim = x[1]

    self.W = self.init((self.input_dim, 4 * self.output_dim))
    self.U = self.inner_init((self.output_dim, 4 * self.output_dim))

    self.b = model_ops.variable(np.hstack(
        (np.zeros(self.output_dim),
         np.ones(self.output_dim),
         np.zeros(self.output_dim),
         np.zeros(self.output_dim))))
    self.trainable_weights = [self.W, self.U, self.b]

  def get_output_shape_for(self, input_shape):
    x, h_tm1, c_tm1 = input_shape # Unpack
    return [(x[0], self.output_dim), h_tm1, c_tm1]

  def call(self, x_states, mask=None):
    x, h_tm1, c_tm1 = x_states # Unpack

    # Taken from Keras code [citation needed]
    z = model_ops.dot(x, self.W) + model_ops.dot(h_tm1, self.U) + self.b

    z0 = z[:, :self.output_dim]
    z1 = z[:, self.output_dim: 2 * self.output_dim]
    z2 = z[:, 2 * self.output_dim: 3 * self.output_dim]
    z3 = z[:, 3 * self.output_dim:]

    i = self.inner_activation(z0)
    f = self.inner_activation(z1)
    c = f * c_tm1 + i * self.activation(z2)
    o = self.inner_activation(z3)

    h = o * self.activation(c)
    
    return o, [h, c]

