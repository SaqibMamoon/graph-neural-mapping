import os
import numpy as np
import pandas as pd


# Class of behavioral measurments
class DataBehavioral(object):
    def __init__(self):
        super(DataBehavioral, self).__init__()
        self.df = pd.read_csv('data/behavioral/hcp.csv').set_index('Subject')

    def get_feature(self, feature=['Gender']):
        behavioral_features = self.df[feature].to_dict()
        behavioral_label = {}

        for f in feature:
            behavioral_label[f] = {}
            seen = []
            for v in behavioral_features[f].values():
                if v not in seen:
                    seen.append(v)
            seen.sort()
            for k, v in behavioral_features[f].items():
                label = seen.index(v)
                behavioral_label[f][k] = label

        return behavioral_features, behavioral_label # dict {subject: feature_string} / dict {subject: label}


# Class of nodes, i.e. ROI features
class DataNodes(object):
    def __init__(self, roi):
        super(DataNodes, self).__init__()
        self.df = pd.read_csv('data/roi/{}.txt'.format(roi), index_col=0, header=None, delimiter='\t')
        self.features = self.df[1].str.split("_", expand=True)
        self.features.columns = ['YeoNetwork', 'Hemisphere', 'Network', 'Region', 'Index']
        self.df_coord = pd.read_csv('data/roi/{}_coord.csv'.format(roi), index_col=0)[1:]

        for i in self.features.index:
            row = self.features.loc[i]
            if row.isnull().any():
                row[4] = row[3]
                row[3] = row[2]

    def __call__(self, subject):
        self.df_timeseries = pd.read_csv('data/timeseries/{}.txt'.format(subject), index_col=False, header=None, delimiter='\t').dropna(axis='columns').to_numpy()

    def get_feature(self, type): # List of 'YeoNetwork', 'Hemisphere', 'Network', 'Region', 'Index'
        feature=['Hemisphere', 'Region', 'Network', 'Index']

        hemisphere_seen = []
        hemisphere_dict = {}
        hemisphere_features = self.features['Hemisphere'].to_dict()
        for v in hemisphere_features.values():
            if v not in hemisphere_seen:
                hemisphere_seen.append(v)
                hemisphere_dict[v] = hemisphere_seen.index(v)
        hemisphere_features = len(hemisphere_seen)
        assert hemisphere_features == 2

        region_seen = []
        region_dict = {}
        region_features = self.features['Region'].to_dict()
        for v in region_features.values():
            if v not in region_seen:
                region_seen.append(v)
                region_dict[v] = region_seen.index(v)
        region_features = len(region_seen)

        network_seen = []
        network_dict = {}
        network_features = self.features['Network'].to_dict()
        for v in network_features.values():
            if v not in network_seen:
                network_seen.append(v)
                network_dict[v] = network_seen.index(v)
        network_features = len(network_seen)

        if type=='one_hot':
            filtered_features = self.features[feature]
            node_features = filtered_features.apply(lambda x: '_'.join(x), axis='columns').to_dict()
            node_label = {}

            seen = []
            for v in node_features.values():
                if v not in seen:
                    seen.append(v)
            for k, v in node_features.items():
                label = seen.index(v)
                node_label[k] = label
            return node_features, node_label # dict {roi: feature_string} / dict {roi: label_value}

        elif type=='coordinate':
            filtered_features = self.df_coord[['R','A','S']]
            node_label_dict = filtered_features.to_dict()
            node_label = {}
            for k in node_label_dict['R'].keys():
                node_label[k] = (node_label_dict['R'][k], node_label_dict['A'][k], node_label_dict['S'][k])
            return node_label_dict, node_label # dict {R,A,S:{roi: coordinate}} / dict {roi: tuple (R,A,S) coordinate}

        elif type=='mean_bold':
            node_label_numpy = np.mean(self.df_timeseries, axis=0)
            node_label_numpy = (node_label_numpy - node_label_numpy.mean()) / (node_label_numpy.std() + 1e-8)
            node_label = {}
            for i, timeseries in enumerate(node_label_numpy):
                node_label[i] = tuple([timeseries])
            return node_label_numpy, node_label

        elif type=='mean_bold_onehot':
            node_label_numpy = np.mean(self.df_timeseries, axis=0)/10000.0
            node_label = {}
            for i, mean_timeseries in enumerate(node_label_numpy):
                vec = np.zeros([len(node_label_numpy)])
                vec[i] = mean_timeseries
                node_label[i] = vec
            return node_label_numpy, node_label

        elif type=='mean_bold_onehot_features':
            node_label_numpy = np.mean(self.df_timeseries, axis=0)/10000.0
            node_label = {}
            for i, mean_timeseries in enumerate(node_label_numpy):
                vec_node = np.zeros([len(node_label_numpy)])
                vec_node[i] = mean_timeseries
                vec_hemisphere = np.zeros(hemisphere_features)
                vec_hemisphere[hemisphere_dict[self.features['Hemisphere'].iloc[i]]] = 1
                vec_region = np.zeros(region_features)
                vec_region[region_dict[self.features['Region'].iloc[i]]] = 1
                vec_network = np.zeros(network_features)
                vec_network[network_dict[self.features['Network'].iloc[i]]] = 1

                node_label[i] = np.concatenate([vec_node, vec_hemisphere, vec_region, vec_network])
            return node_label_numpy, node_label

        else:
            raise Exception('unknown node feature type')


# Class of edges, i.e. FC features
class DataEdges(object):
    def __init__(self):
        super(DataEdges, self).__init__()
        # self.df = pd.read_csv('data/connectivity/{}.txt'.format(subject), index_col=False, header=None, delimiter='\t').dropna(axis='columns').to_numpy()

    def __call__(self, preprocessing, run, rois, subject):
        self.df = pd.read_csv('data/connectivity/{}/{}/{}/r{}.txt'.format(preprocessing, run, rois, subject), index_col=False, header=None, delimiter='\t').dropna(axis='columns').to_numpy()

    def get_adjacency(self, threshold):
        if threshold is None:
            return self.df
        mask = (self.df > np.percentile(self.df, threshold)).astype(np.uint8)
        nodes, neighbors = np.nonzero(mask)
        sparse_mask = {}
        for i, node in enumerate(nodes):
            if neighbors[i] > node:
                if not node in sparse_mask: sparse_mask[node] = [neighbors[i]]
                else: sparse_mask[node].append(neighbors[i])
        return mask, sparse_mask # matrix adjacency / dict {roi: neighbor_roi}