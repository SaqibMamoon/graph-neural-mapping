import torch
import torch.nn as nn
import torch.nn.functional as F
import torch_geometric as G
import numpy as np

import sys
sys.path.append("models/")
from mlp import MLP
from discriminator import Discriminator
from classifier import Classifier


class GIN_InfoMaxReg(nn.Module):
    def __init__(self, num_layers, num_mlp_layers, input_dim, hidden_dim, output_dim, final_dropout, dropout_layers, learn_eps, graph_pooling_type, neighbor_pooling_type, device):
        '''
            num_layers: number of layers in the neural networks (INCLUDING the input layer)
            num_mlp_layers: number of layers in mlps (EXCLUDING the input layer)
            input_dim: dimensionality of input features
            hidden_dim: dimensionality of hidden units at ALL layers
            output_dim: number of classes for prediction
            final_dropout: dropout ratio on the final linear layer
            learn_eps: If True, learn epsilon to distinguish center nodes from neighboring nodes. If False, aggregate neighbors and center nodes altogether.
            neighbor_pooling_type: how to aggregate neighbors (mean, average, or max)
            graph_pooling_type: how to aggregate entire nodes in a graph (mean, average)
            device: which device to use
        '''

        super(GIN_InfoMaxReg, self).__init__()

        self.disc = Discriminator(hidden_dim*num_layers)
        self.sigm = nn.Sigmoid()
        self.relu = nn.ReLU()

        self.final_dropout = final_dropout
        self.dropout_layers = dropout_layers
        self.device = device
        self.num_layers = num_layers
        self.graph_pooling_type = graph_pooling_type
        self.neighbor_pooling_type = neighbor_pooling_type
        self.learn_eps = learn_eps
        self.eps = nn.Parameter(torch.zeros(num_layers))

        ###List of MLPs
        self.mlps = torch.nn.ModuleList()

        ###List of batchnorms applied to the output of MLP (input of the final prediction linear layer)
        self.batch_norms = torch.nn.ModuleList()

        for layer in range(num_layers):
            if layer == 0:
                self.mlps.append(MLP(num_mlp_layers, input_dim, hidden_dim, hidden_dim))
            else:
                self.mlps.append(MLP(num_mlp_layers, hidden_dim, hidden_dim, hidden_dim))

            self.batch_norms.append(nn.BatchNorm1d(hidden_dim))

        #Linear function that maps the hidden representation at dofferemt layers into a prediction score
        self.linears_prediction = torch.nn.ModuleList()
        for layer in range(num_layers):
            self.linears_prediction.append(nn.Linear(hidden_dim, output_dim))


    def __preprocess_neighbors_maxpool(self, batch_graph):
        ###create padded_neighbor_list in concatenated graph

        #compute the maximum number of neighbors within the graphs in the current minibatch
        max_deg = max([graph.max_neighbor for graph in batch_graph])

        padded_neighbor_list = []
        start_idx = [0]


        for i, graph in enumerate(batch_graph):
            start_idx.append(start_idx[i] + len(graph.g))
            padded_neighbors = []
            for j in range(len(graph.neighbors)):
                #add off-set values to the neighbor indices
                pad = [n + start_idx[i] for n in graph.neighbors[j]]
                #padding, dummy data is assumed to be stored in -1
                pad.extend([-1]*(max_deg - len(pad)))

                #Add center nodes in the maxpooling if learn_eps is False, i.e., aggregate center nodes and neighbor nodes altogether.
                if not self.learn_eps:
                    pad.append(j + start_idx[i])

                padded_neighbors.append(pad)
            padded_neighbor_list.extend(padded_neighbors)

        return torch.LongTensor(padded_neighbor_list)


    def __preprocess_neighbors_sumavepool(self, batch_graph):
        ###create block diagonal sparse matrix

        edge_mat_list = []
        start_idx = [0]
        for i, graph in enumerate(batch_graph):
            start_idx.append(start_idx[i] + len(graph.g))
            edge_mat_list.append(graph.edge_mat + start_idx[i])
        Adj_block_idx = torch.cat(edge_mat_list, 1)
        Adj_block_elem = torch.ones(Adj_block_idx.shape[1])

        #Add self-loops in the adjacency matrix if learn_eps is False, i.e., aggregate center nodes and neighbor nodes altogether.

        if not self.learn_eps:
            num_node = start_idx[-1]
            self_loop_edge = torch.LongTensor([range(num_node), range(num_node)])
            elem = torch.ones(num_node)
            Adj_block_idx = torch.cat([Adj_block_idx, self_loop_edge], 1)
            Adj_block_elem = torch.cat([Adj_block_elem, elem], 0)

        Adj_block = torch.sparse.FloatTensor(Adj_block_idx, Adj_block_elem, torch.Size([start_idx[-1],start_idx[-1]]))

        return Adj_block.to(self.device)


    def __preprocess_graphpool(self, batch_graph):
        ###create sum or average pooling sparse matrix over entire nodes in each graph (num graphs x num nodes)

        start_idx = [0]

        #compute the padded neighbor list
        for i, graph in enumerate(batch_graph):
            start_idx.append(start_idx[i] + len(graph.g))

        idx = []
        elem = []
        for i, graph in enumerate(batch_graph):
            ###average pooling
            if self.graph_pooling_type == "average":
                elem.extend([1./len(graph.g)]*len(graph.g))

            else:
            ###sum pooling
                elem.extend([1]*len(graph.g))

            idx.extend([[i, j] for j in range(start_idx[i], start_idx[i+1], 1)])
        elem = torch.FloatTensor(elem)
        idx = torch.LongTensor(idx).transpose(0,1)
        graph_pool = torch.sparse.FloatTensor(idx, elem, torch.Size([len(batch_graph), start_idx[-1]]))

        return graph_pool.to(self.device)

    def maxpool(self, h, padded_neighbor_list):
        ###Element-wise minimum will never affect max-pooling

        dummy = torch.min(h, dim = 0)[0]
        h_with_dummy = torch.cat([h, dummy.reshape((1, -1)).to(self.device)])
        pooled_rep = torch.max(h_with_dummy[padded_neighbor_list], dim = 1)[0]
        return pooled_rep


    def next_layer_eps(self, h, layer, padded_neighbor_list = None, Adj_block = None):
        ###pooling neighboring nodes and center nodes separately by epsilon reweighting.

        if self.neighbor_pooling_type == "max":
            ##If max pooling
            pooled = self.maxpool(h, padded_neighbor_list)
        else:
            #If sum or average pooling
            pooled = torch.spmm(Adj_block, h)
            if self.neighbor_pooling_type == "average":
                #If average pooling
                degree = torch.spmm(Adj_block, torch.ones((Adj_block.shape[0], 1)).to(self.device))
                pooled = pooled/degree

        #Reweights the center node representation when aggregating it with its neighbors
        pooled = pooled + (1 + self.eps[layer])*h
        pooled_rep = self.mlps[layer](pooled)
        h = self.batch_norms[layer](pooled_rep)

        #non-linearity
        h = self.relu(h)
        return h


    def next_layer(self, h, layer, padded_neighbor_list = None, Adj_block = None):
        ###pooling neighboring nodes and center nodes altogether

        if self.neighbor_pooling_type == "max":
            ##If max pooling
            pooled = self.maxpool(h, padded_neighbor_list)
        else:
            #If sum or average pooling
            pooled = torch.spmm(Adj_block, h)
            if self.neighbor_pooling_type == "average":
                #If average pooling
                degree = torch.spmm(Adj_block, torch.ones((Adj_block.shape[0], 1)).to(self.device))
                pooled = pooled/degree

        #representation of neighboring and center nodes
        pooled_rep = self.mlps[layer](pooled)

        h = self.batch_norms[layer](pooled_rep)

        #non-linearity
        h = self.relu(h)
        return h


    def forward(self, batch_graph, latent=False):
        X_concat = torch.cat([graph.node_features for graph in batch_graph], 0).to(self.device) # [557,7] ==> [concatenated nodes in batch_graph , node_features]
        X_concat.requires_grad_()
        self._last_input = X_concat
        graph_pool = self.__preprocess_graphpool(batch_graph) # [32, 557]

        idx = []
        rand_seq = np.random.permutation(len(batch_graph))
        for i in rand_seq:
            idx += [i]*len(batch_graph[0].node_features) #[graph index in minibatch as label]

        if self.neighbor_pooling_type == "max":
            padded_neighbor_list = self.__preprocess_neighbors_maxpool(batch_graph)
        else:
            Adj_block = self.__preprocess_neighbors_sumavepool(batch_graph)

        #list of hidden representation at each layer (including input)
        hidden_rep = []
        h = X_concat

        for layer in range(self.num_layers):
            if self.neighbor_pooling_type == "max" and self.learn_eps:
                h = self.next_layer_eps(h, layer, padded_neighbor_list = padded_neighbor_list)
                if str(layer) in self.dropout_layers:
                    h = F.dropout(h, 0.5, training=self.training)
            elif not self.neighbor_pooling_type == "max" and self.learn_eps:
                h = self.next_layer_eps(h, layer, Adj_block = Adj_block)
                if str(layer) in self.dropout_layers:
                    h = F.dropout(h, 0.5, training=self.training)
            elif self.neighbor_pooling_type == "max" and not self.learn_eps:
                h = self.next_layer(h, layer, padded_neighbor_list = padded_neighbor_list)
                if str(layer) in self.dropout_layers:
                    h = F.dropout(h, 0.5, training=self.training)
            elif not self.neighbor_pooling_type == "max" and not self.learn_eps:
                h = self.next_layer(h, layer, Adj_block = Adj_block)
                if str(layer) in self.dropout_layers:
                    h = F.dropout(h, 0.5, training=self.training)

            hidden_rep.append(h) # [[557,7],[557,64]x4]

        c_logit = 0

        graph_latent = []
        #perform pooling over all nodes in each graph in every layer
        for layer, h in enumerate(hidden_rep):
            pooled_h = torch.spmm(graph_pool, h) # [32,7], [32,64]x4
            c_logit += F.dropout(self.linears_prediction[layer](pooled_h), self.final_dropout, training = self.training) # [32,2]
            graph_latent.append(pooled_h)

        n_f = torch.cat(hidden_rep, 1) #12800,256
        g_f = torch.cat(graph_latent, 1) #32,256
        # n_idx = torch.tensor().to(self.device)

        h_1 = n_f

        c = g_f
        c = self.sigm(c)

        idx = np.asarray(idx)
        shuf_n_f = n_f[idx, :]

        h_2 = shuf_n_f

        d_logit = self.disc(c, h_1, h_2, None, None)

        if latent:
            return g_f.detach().cpu().numpy()
        else:
            return c_logit, d_logit

    def compute_saliency(self, batch_graph, cls):
        self.eval()
        self.zero_grad()
        assert len(batch_graph)==1
        X_concat = torch.cat([graph.node_features for graph in batch_graph], 0).to(self.device) # [557,7] ==> [concatenated nodes in batch_graph , node_features]
        X_concat.requires_grad_()
        graph_pool = self.__preprocess_graphpool(batch_graph) # [32, 557]

        # predicting 0
        predicting_class = torch.zeros([1,2]).to(self.device)
        predicting_class[0, cls] = 1

        if self.neighbor_pooling_type == "max":
            padded_neighbor_list = self.__preprocess_neighbors_maxpool(batch_graph)
        else:
            Adj_block = self.__preprocess_neighbors_sumavepool(batch_graph)

        #list of hidden representation at each layer (including input)
        hidden_rep = []
        h = X_concat

        for layer in range(self.num_layers):
            if self.neighbor_pooling_type == "max" and self.learn_eps:
                h = self.next_layer_eps(h, layer, padded_neighbor_list = padded_neighbor_list)
            elif not self.neighbor_pooling_type == "max" and self.learn_eps:
                h = self.next_layer_eps(h, layer, Adj_block = Adj_block)
            elif self.neighbor_pooling_type == "max" and not self.learn_eps:
                h = self.next_layer(h, layer, padded_neighbor_list = padded_neighbor_list)
            elif not self.neighbor_pooling_type == "max" and not self.learn_eps:
                h = self.next_layer(h, layer, Adj_block = Adj_block)
            h.retain_grad()
            hidden_rep.append(h) # [[557,7],[557,64]x4]

        score_over_layer = 0
        class_activation = torch.zeros([X_concat.shape[0]]).to(self.device)
        grad_class_activation = torch.zeros([X_concat.shape[0]]).to(self.device)

        #perform pooling over all nodes in each graph in every layer
        for layer, h in enumerate(hidden_rep):
            pooled_h = torch.spmm(graph_pool, h) # [32,7], [32,64]x4
            score_over_layer += F.dropout(self.linears_prediction[layer](pooled_h), self.final_dropout, training = self.training) # [32,2]
            weight = self.linears_prediction[layer].weight[cls]
            class_activation += torch.matmul(h,weight)

            score_over_layer[0,cls].backward(retain_graph=True)
            grad_feature = h.grad[0]
            grad_class_activation += torch.matmul(h, grad_feature)

        score_over_layer.backward(predicting_class)
        saliency = X_concat.grad

        return saliency, class_activation, self.relu(grad_class_activation)


class GCN_InfoMaxReg(nn.Module):
    def __init__(self, num_layers, num_mlp_layers, input_dim, hidden_dim, output_dim, final_dropout, dropout_layers, learn_eps, graph_pooling_type, neighbor_pooling_type, device):
        '''
            num_layers: number of layers in the neural networks (INCLUDING the input layer)
            num_mlp_layers: number of layers in mlps (EXCLUDING the input layer)
            input_dim: dimensionality of input features
            hidden_dim: dimensionality of hidden units at ALL layers
            output_dim: number of classes for prediction
            final_dropout: dropout ratio on the final linear layer
            learn_eps: If True, learn epsilon to distinguish center nodes from neighboring nodes. If False, aggregate neighbors and center nodes altogether.
            neighbor_pooling_type: how to aggregate neighbors (mean, average, or max)
            graph_pooling_type: how to aggregate entire nodes in a graph (mean, average)
            device: which device to use
        '''

        super(GCN_InfoMaxReg, self).__init__()

        self.disc = Discriminator(128)
        self.sigm = nn.Sigmoid()
        self.relu = nn.ReLU()

        self.final_dropout = final_dropout
        self.dropout_layers = dropout_layers
        self.device = device
        self.num_layers = num_layers
        self.graph_pooling_type = graph_pooling_type
        self.neighbor_pooling_type = neighbor_pooling_type
        self.learn_eps = learn_eps
        self.eps = nn.Parameter(torch.zeros(self.num_layers))

        ###List of MLPs
        self.mlps = torch.nn.ModuleList()

        ###List of batchnorms applied to the output of MLP (input of the final prediction linear layer)
        self.batch_norms = torch.nn.ModuleList()
        self.linears_prediction = torch.nn.ModuleList()

        for layer in range(self.num_layers):
            if layer == 0:
                self.mlps.append(MLP(1, input_dim, 32, 32))
                self.batch_norms.append(nn.BatchNorm1d(32))
                self.linears_prediction.append(nn.Linear(32, output_dim))
            elif layer == 1:
                self.mlps.append(MLP(1, 32, 32, 32))
                self.batch_norms.append(nn.BatchNorm1d(32))
                self.linears_prediction.append(nn.Linear(32, output_dim))
            elif layer == 2:
                self.mlps.append(MLP(1, 32, 64, 64))
                self.batch_norms.append(nn.BatchNorm1d(64))
                self.linears_prediction.append(nn.Linear(64, output_dim))
            elif layer == 3:
                self.mlps.append(MLP(1, 64, 64, 64))
                self.batch_norms.append(nn.BatchNorm1d(64))
                self.linears_prediction.append(nn.Linear(64, output_dim))
            elif layer == 4:
                self.mlps.append(MLP(1, 64, 64, 128))
                self.batch_norms.append(nn.BatchNorm1d(128))
                self.linears_prediction.append(nn.Linear(128, output_dim))
            else:
                raise Exception('num_layers exceeds baseline')


        #Linear function that maps the hidden representation at dofferemt layers into a prediction score


    def __preprocess_neighbors_maxpool(self, batch_graph):
        ###create padded_neighbor_list in concatenated graph

        #compute the maximum number of neighbors within the graphs in the current minibatch
        max_deg = max([graph.max_neighbor for graph in batch_graph])

        padded_neighbor_list = []
        start_idx = [0]


        for i, graph in enumerate(batch_graph):
            start_idx.append(start_idx[i] + len(graph.g))
            padded_neighbors = []
            for j in range(len(graph.neighbors)):
                #add off-set values to the neighbor indices
                pad = [n + start_idx[i] for n in graph.neighbors[j]]
                #padding, dummy data is assumed to be stored in -1
                pad.extend([-1]*(max_deg - len(pad)))

                #Add center nodes in the maxpooling if learn_eps is False, i.e., aggregate center nodes and neighbor nodes altogether.
                if not self.learn_eps:
                    pad.append(j + start_idx[i])

                padded_neighbors.append(pad)
            padded_neighbor_list.extend(padded_neighbors)

        return torch.LongTensor(padded_neighbor_list)


    def __preprocess_neighbors_sumavepool(self, batch_graph):
        ###create block diagonal sparse matrix

        edge_mat_list = []
        start_idx = [0]
        for i, graph in enumerate(batch_graph):
            start_idx.append(start_idx[i] + len(graph.g))
            edge_mat_list.append(graph.edge_mat + start_idx[i])
        Adj_block_idx = torch.cat(edge_mat_list, 1)
        Adj_block_elem = torch.ones(Adj_block_idx.shape[1])

        #Add self-loops in the adjacency matrix if learn_eps is False, i.e., aggregate center nodes and neighbor nodes altogether.

        if not self.learn_eps:
            num_node = start_idx[-1]
            self_loop_edge = torch.LongTensor([range(num_node), range(num_node)])
            elem = torch.ones(num_node)
            Adj_block_idx = torch.cat([Adj_block_idx, self_loop_edge], 1)
            Adj_block_elem = torch.cat([Adj_block_elem, elem], 0)

        Adj_block = torch.sparse.FloatTensor(Adj_block_idx, Adj_block_elem, torch.Size([start_idx[-1],start_idx[-1]]))

        return Adj_block.to(self.device)


    def __preprocess_graphpool(self, batch_graph):
        ###create sum or average pooling sparse matrix over entire nodes in each graph (num graphs x num nodes)

        start_idx = [0]

        #compute the padded neighbor list
        for i, graph in enumerate(batch_graph):
            start_idx.append(start_idx[i] + len(graph.g))

        idx = []
        elem = []
        for i, graph in enumerate(batch_graph):
            ###average pooling
            if self.graph_pooling_type == "average":
                elem.extend([1./len(graph.g)]*len(graph.g))

            else:
            ###sum pooling
                elem.extend([1]*len(graph.g))

            idx.extend([[i, j] for j in range(start_idx[i], start_idx[i+1], 1)])
        elem = torch.FloatTensor(elem)
        idx = torch.LongTensor(idx).transpose(0,1)
        graph_pool = torch.sparse.FloatTensor(idx, elem, torch.Size([len(batch_graph), start_idx[-1]]))

        return graph_pool.to(self.device)

    def maxpool(self, h, padded_neighbor_list):
        ###Element-wise minimum will never affect max-pooling

        dummy = torch.min(h, dim = 0)[0]
        h_with_dummy = torch.cat([h, dummy.reshape((1, -1)).to(self.device)])
        pooled_rep = torch.max(h_with_dummy[padded_neighbor_list], dim = 1)[0]
        return pooled_rep


    def next_layer_eps(self, h, layer, padded_neighbor_list = None, Adj_block = None):
        ###pooling neighboring nodes and center nodes separately by epsilon reweighting.

        if self.neighbor_pooling_type == "max":
            ##If max pooling
            pooled = self.maxpool(h, padded_neighbor_list)
        else:
            #If sum or average pooling
            pooled = torch.spmm(Adj_block, h)
            if self.neighbor_pooling_type == "average":
                #If average pooling
                degree = torch.spmm(Adj_block, torch.ones((Adj_block.shape[0], 1)).to(self.device))
                pooled = pooled/degree

        #Reweights the center node representation when aggregating it with its neighbors
        pooled = pooled + (1 + self.eps[layer])*h
        pooled_rep = self.mlps[layer](pooled)
        h = self.batch_norms[layer](pooled_rep)

        #non-linearity
        h = self.relu(h)
        return h


    def next_layer(self, h, layer, padded_neighbor_list = None, Adj_block = None):
        ###pooling neighboring nodes and center nodes altogether

        if self.neighbor_pooling_type == "max":
            ##If max pooling
            pooled = self.maxpool(h, padded_neighbor_list)
        else:
            #If sum or average pooling
            pooled = torch.spmm(Adj_block, h)
            if self.neighbor_pooling_type == "average":
                #If average pooling
                degree = torch.spmm(Adj_block, torch.ones((Adj_block.shape[0], 1)).to(self.device))
                pooled = pooled/degree

        #representation of neighboring and center nodes
        pooled_rep = self.mlps[layer](pooled)

        h = self.batch_norms[layer](pooled_rep)

        #non-linearity
        h = self.relu(h)
        return h


    def forward(self, batch_graph, latent=False):
        X_concat = torch.cat([graph.node_features for graph in batch_graph], 0).to(self.device) # [557,7] ==> [concatenated nodes in batch_graph , node_features]
        graph_pool = self.__preprocess_graphpool(batch_graph) # [32, 557]

        idx = []
        rand_seq = np.random.permutation(len(batch_graph))
        for i in rand_seq:
            idx += [i]*len(batch_graph[0].node_features) #[graph index in minibatch as label]

        if self.neighbor_pooling_type == "max":
            padded_neighbor_list = self.__preprocess_neighbors_maxpool(batch_graph)
        else:
            Adj_block = self.__preprocess_neighbors_sumavepool(batch_graph)

        #list of hidden representation at each layer (including input)
        hidden_rep = []
        h = X_concat

        for layer in range(self.num_layers):
            if self.neighbor_pooling_type == "max" and self.learn_eps:
                h = self.next_layer_eps(h, layer, padded_neighbor_list = padded_neighbor_list)
                if str(layer) in self.dropout_layers:
                    h = F.dropout(h, 0.5, training=self.training)
            elif not self.neighbor_pooling_type == "max" and self.learn_eps:
                h = self.next_layer_eps(h, layer, Adj_block = Adj_block)
                if str(layer) in self.dropout_layers:
                    h = F.dropout(h, 0.5, training=self.training)
            elif self.neighbor_pooling_type == "max" and not self.learn_eps:
                h = self.next_layer(h, layer, padded_neighbor_list = padded_neighbor_list)
                if str(layer) in self.dropout_layers:
                    h = F.dropout(h, 0.5, training=self.training)
            elif not self.neighbor_pooling_type == "max" and not self.learn_eps:
                h = self.next_layer(h, layer, Adj_block = Adj_block)
                if str(layer) in self.dropout_layers:
                    h = F.dropout(h, 0.5, training=self.training)

            hidden_rep.append(h) # [[557,7],[557,64]x4]

        pooled_h = torch.spmm(graph_pool, h) # [32,7], [32,64]x4
        c_logit = F.dropout(self.linears_prediction[-1](pooled_h), self.final_dropout, training=self.training)

        n_f = h
        g_f = pooled_h

        h_1 = n_f

        c = g_f
        c = self.sigm(c)

        idx = np.asarray(idx)
        shuf_n_f = n_f[idx, :]

        h_2 = shuf_n_f

        d_logit = self.disc(c, h_1, h_2, None, None)

        if latent:
            return g_f.detach().cpu().numpy()
        else:
            return c_logit, d_logit

    def compute_saliency(self, batch_graph, cls):
        self.eval()
        assert len(batch_graph)==1
        X_concat = torch.cat([graph.node_features for graph in batch_graph], 0).to(self.device) # [557,7] ==> [concatenated nodes in batch_graph , node_features]
        X_concat.requires_grad_()
        graph_pool = self.__preprocess_graphpool(batch_graph) # [32, 557]

        if self.neighbor_pooling_type == "max":
            padded_neighbor_list = self.__preprocess_neighbors_maxpool(batch_graph)
        else:
            Adj_block = self.__preprocess_neighbors_sumavepool(batch_graph)

        #list of hidden representation at each layer (including input)
        hidden_rep = []
        h = X_concat

        for layer in range(self.num_layers):
            if self.neighbor_pooling_type == "max" and self.learn_eps:
                h = self.next_layer_eps(h, layer, padded_neighbor_list = padded_neighbor_list)
            elif not self.neighbor_pooling_type == "max" and self.learn_eps:
                h = self.next_layer_eps(h, layer, Adj_block = Adj_block)
            elif self.neighbor_pooling_type == "max" and not self.learn_eps:
                h = self.next_layer(h, layer, padded_neighbor_list = padded_neighbor_list)
            elif not self.neighbor_pooling_type == "max" and not self.learn_eps:
                h = self.next_layer(h, layer, Adj_block = Adj_block)

            hidden_rep.append(h) # [[557,7],[557,64]x4]

        pooled_h = torch.spmm(graph_pool, h) # [32,7], [32,64]x4
        score_over_layer = self.linears_prediction[-1](pooled_h)

        weight = self.linears_prediction[-1].weight[cls]
        class_activation += torch.matmul(h,weight)

        # predicting 0
        predicting_class = torch.zeros_like(score_over_layer).to(self.device)
        predicting_class[0, cls] = 1
        score_over_layer.backward(predicting_class)
        saliency = X_concat.grad

        return saliency, class_activation

class GCN_Cheb(nn.Module):
    def __init__(self, num_layers, num_mlp_layers, input_dim, hidden_dim, output_dim, final_dropout, dropout_layers, learn_eps, graph_pooling_type, neighbor_pooling_type, device):
        '''
            num_layers: number of layers in the neural networks (INCLUDING the input layer)
            num_mlp_layers: number of layers in mlps (EXCLUDING the input layer)
            input_dim: dimensionality of input features
            hidden_dim: dimensionality of hidden units at ALL layers
            output_dim: number of classes for prediction
            final_dropout: dropout ratio on the final linear layer
            learn_eps: If True, learn epsilon to distinguish center nodes from neighboring nodes. If False, aggregate neighbors and center nodes altogether.
            neighbor_pooling_type: how to aggregate neighbors (mean, average, or max)
            graph_pooling_type: how to aggregate entire nodes in a graph (mean, average)
            device: which device to use
        '''

        super(GCN_Cheb, self).__init__()

        self.final_dropout = final_dropout
        self.dropout_layers = dropout_layers
        self.device = device
        self.num_layers = num_layers
        self.graph_pooling_type = graph_pooling_type
        self.neighbor_pooling_type = neighbor_pooling_type
        self.learn_eps = learn_eps
        self.eps = nn.Parameter(torch.zeros(self.num_layers))

        ###List of MLPs
        self.mlps = torch.nn.ModuleList()

        ###List of batchnorms applied to the output of MLP (input of the final prediction linear layer)
        self.batch_norms = torch.nn.ModuleList()
        self.linears_prediction = torch.nn.ModuleList()

        for layer in range(self.num_layers):
            if layer == 0:
                self.mlps.append(G.nn.ChebConv(input_dim, 32, 9))
                self.batch_norms.append(nn.BatchNorm1d(32))
                self.linears_prediction.append(nn.Linear(32, output_dim))
            elif layer == 1:
                self.mlps.append(G.nn.ChebConv(32, 32, 9))
                self.batch_norms.append(nn.BatchNorm1d(32))
                self.linears_prediction.append(nn.Linear(32, output_dim))
            elif layer == 2:
                self.mlps.append(G.nn.ChebConv(32, 64, 9))
                self.batch_norms.append(nn.BatchNorm1d(64))
                self.linears_prediction.append(nn.Linear(64, output_dim))
            elif layer == 3:
                self.mlps.append(G.nn.ChebConv(64, 64, 9))
                self.batch_norms.append(nn.BatchNorm1d(64))
                self.linears_prediction.append(nn.Linear(64, output_dim))
            elif layer == 4:
                self.mlps.append(G.nn.ChebConv(64, 128, 9))
                self.batch_norms.append(nn.BatchNorm1d(128))
                self.linears_prediction.append(nn.Linear(128, output_dim))
            else:
                raise Exception('num_layers exceeds baseline')


        #Linear function that maps the hidden representation at dofferemt layers into a prediction score


    def __preprocess_neighbors_maxpool(self, batch_graph):
        ###create padded_neighbor_list in concatenated graph

        #compute the maximum number of neighbors within the graphs in the current minibatch
        max_deg = max([graph.max_neighbor for graph in batch_graph])

        padded_neighbor_list = []
        start_idx = [0]


        for i, graph in enumerate(batch_graph):
            start_idx.append(start_idx[i] + len(graph.g))
            padded_neighbors = []
            for j in range(len(graph.neighbors)):
                #add off-set values to the neighbor indices
                pad = [n + start_idx[i] for n in graph.neighbors[j]]
                #padding, dummy data is assumed to be stored in -1
                pad.extend([-1]*(max_deg - len(pad)))

                #Add center nodes in the maxpooling if learn_eps is False, i.e., aggregate center nodes and neighbor nodes altogether.
                if not self.learn_eps:
                    pad.append(j + start_idx[i])

                padded_neighbors.append(pad)
            padded_neighbor_list.extend(padded_neighbors)

        return torch.LongTensor(padded_neighbor_list)


    def __preprocess_neighbors_sumavepool(self, batch_graph):
        ###create block diagonal sparse matrix

        edge_mat_list = []
        start_idx = [0]
        for i, graph in enumerate(batch_graph):
            start_idx.append(start_idx[i] + len(graph.g))
            edge_mat_list.append(graph.edge_mat + start_idx[i])
        Adj_block_idx = torch.cat(edge_mat_list, 1)
        Adj_block_elem = torch.ones(Adj_block_idx.shape[1])

        #Add self-loops in the adjacency matrix if learn_eps is False, i.e., aggregate center nodes and neighbor nodes altogether.

        if not self.learn_eps:
            num_node = start_idx[-1]
            self_loop_edge = torch.LongTensor([range(num_node), range(num_node)])
            elem = torch.ones(num_node)
            Adj_block_idx = torch.cat([Adj_block_idx, self_loop_edge], 1)
            Adj_block_elem = torch.cat([Adj_block_elem, elem], 0)

        Adj_block = torch.sparse.FloatTensor(Adj_block_idx, Adj_block_elem, torch.Size([start_idx[-1],start_idx[-1]]))

        return Adj_block.to(self.device)


    def __preprocess_graphpool(self, batch_graph):
        ###create sum or average pooling sparse matrix over entire nodes in each graph (num graphs x num nodes)

        start_idx = [0]

        #compute the padded neighbor list
        for i, graph in enumerate(batch_graph):
            start_idx.append(start_idx[i] + len(graph.g))

        idx = []
        elem = []
        for i, graph in enumerate(batch_graph):
            ###average pooling
            if self.graph_pooling_type == "average":
                elem.extend([1./len(graph.g)]*len(graph.g))

            else:
            ###sum pooling
                elem.extend([1]*len(graph.g))

            idx.extend([[i, j] for j in range(start_idx[i], start_idx[i+1], 1)])
        elem = torch.FloatTensor(elem)
        idx = torch.LongTensor(idx).transpose(0,1)
        graph_pool = torch.sparse.FloatTensor(idx, elem, torch.Size([len(batch_graph), start_idx[-1]]))

        return graph_pool.to(self.device)

    def maxpool(self, h, padded_neighbor_list):
        ###Element-wise minimum will never affect max-pooling

        dummy = torch.min(h, dim = 0)[0]
        h_with_dummy = torch.cat([h, dummy.reshape((1, -1)).to(self.device)])
        pooled_rep = torch.max(h_with_dummy[padded_neighbor_list], dim = 1)[0]
        return pooled_rep


    def next_layer_eps(self, h, layer, padded_neighbor_list = None, Adj_block = None):
        ###pooling neighboring nodes and center nodes separately by epsilon reweighting.

        edge_index = Adj_block._indices()
        h = self.mlps[layer](h, edge_index)
        h = self.batch_norms[layer](h)

        #non-linearity
        h = self.relu(h)
        return h


    def next_layer(self, h, layer, padded_neighbor_list = None, Adj_block = None):
        ###pooling neighboring nodes and center nodes altogether
        edge_index = Adj_block._indices()
        h = self.mlps[layer](h, edge_index)
        h = self.batch_norms[layer](h)

        #non-linearity
        h = self.relu(h)
        return h


    def forward(self, batch_graph, latent=False):
        X_concat = torch.cat([graph.node_features for graph in batch_graph], 0).to(self.device) # [557,7] ==> [concatenated nodes in batch_graph , node_features]
        graph_pool = self.__preprocess_graphpool(batch_graph) # [32, 557]

        idx = []
        rand_seq = np.random.permutation(len(batch_graph))
        for i in rand_seq:
            idx += [i]*len(batch_graph[0].node_features) #[graph index in minibatch as label]

        if self.neighbor_pooling_type == "max":
            padded_neighbor_list = self.__preprocess_neighbors_maxpool(batch_graph)
        else:
            Adj_block = self.__preprocess_neighbors_sumavepool(batch_graph)

        #list of hidden representation at each layer (including input)
        hidden_rep = []
        h = X_concat

        for layer in range(self.num_layers):
            if self.neighbor_pooling_type == "max" and self.learn_eps:
                h = self.next_layer_eps(h, layer, padded_neighbor_list = padded_neighbor_list)
                if str(layer) in self.dropout_layers:
                    h = F.dropout(h, 0.5, training=self.training)
            elif not self.neighbor_pooling_type == "max" and self.learn_eps:
                h = self.next_layer_eps(h, layer, Adj_block = Adj_block)
                if str(layer) in self.dropout_layers:
                    h = F.dropout(h, 0.5, training=self.training)
            elif self.neighbor_pooling_type == "max" and not self.learn_eps:
                h = self.next_layer(h, layer, padded_neighbor_list = padded_neighbor_list)
                if str(layer) in self.dropout_layers:
                    h = F.dropout(h, 0.5, training=self.training)
            elif not self.neighbor_pooling_type == "max" and not self.learn_eps:
                h = self.next_layer(h, layer, Adj_block = Adj_block)
                if str(layer) in self.dropout_layers:
                    h = F.dropout(h, 0.5, training=self.training)

            hidden_rep.append(h) # [[557,7],[557,64]x4]

        pooled_h = torch.spmm(graph_pool, h) # [32,7], [32,64]x4
        c_logit = F.dropout(self.linears_prediction[-1](pooled_h), self.final_dropout, training=self.training)


        if latent:
            return pooled_h.detach().cpu().numpy()
        else:
            return c_logit, c_logit

    def compute_saliency(self, batch_graph, cls):
        self.eval()
        assert len(batch_graph)==1
        X_concat = torch.cat([graph.node_features for graph in batch_graph], 0).to(self.device) # [557,7] ==> [concatenated nodes in batch_graph , node_features]
        X_concat.requires_grad_()
        graph_pool = self.__preprocess_graphpool(batch_graph) # [32, 557]

        if self.neighbor_pooling_type == "max":
            padded_neighbor_list = self.__preprocess_neighbors_maxpool(batch_graph)
        else:
            Adj_block = self.__preprocess_neighbors_sumavepool(batch_graph)

        #list of hidden representation at each layer (including input)
        hidden_rep = []
        h = X_concat

        for layer in range(self.num_layers):
            if self.neighbor_pooling_type == "max" and self.learn_eps:
                h = self.next_layer_eps(h, layer, padded_neighbor_list = padded_neighbor_list)
            elif not self.neighbor_pooling_type == "max" and self.learn_eps:
                h = self.next_layer_eps(h, layer, Adj_block = Adj_block)
            elif self.neighbor_pooling_type == "max" and not self.learn_eps:
                h = self.next_layer(h, layer, padded_neighbor_list = padded_neighbor_list)
            elif not self.neighbor_pooling_type == "max" and not self.learn_eps:
                h = self.next_layer(h, layer, Adj_block = Adj_block)

            hidden_rep.append(h) # [[557,7],[557,64]x4]

        pooled_h = torch.spmm(graph_pool, h) # [32,7], [32,64]x4
        score_over_layer = self.linears_prediction[-1](pooled_h)
        class_activation = torch.zeros([X_concat.shape[0]]).to(self.device)

        # predicting 0
        predicting_class = torch.zeros_like(score_over_layer).to(self.device)
        predicting_class[0, cls] = 1
        score_over_layer.backward(predicting_class)
        saliency = X_concat.grad

        return saliency
