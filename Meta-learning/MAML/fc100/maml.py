
#!/usr/bin/env python3

import random

import numpy as np
import torch
from torch import nn
from torch import optim
import torchvision as tv

import learn2learn as l2l
from learn2learn.data.transforms import FusedNWaysKShots, NWays, KShots, LoadData, RemapLabels, ConsecutiveLabels

import pickle

class Lambda(nn.Module):

    def __init__(self, fn):
        super(Lambda, self).__init__()
        self.fn = fn

    def forward(self, x):
        return self.fn(x)

def accuracy(predictions, targets):
    predictions = predictions.argmax(dim=1).view(targets.shape)
    return (predictions == targets).sum().float() / targets.size(0)


def fast_adapt(batch, learner, loss, adaptation_steps, shots, ways, device):
    data, labels = batch
    data, labels = data.to(device), labels.to(device)

    # Separate data into adaptation/evalutation sets
    adaptation_indices = np.zeros(data.size(0), dtype=bool)
    adaptation_indices[np.arange(shots*ways) * 2] = True
    evaluation_indices = torch.from_numpy(~adaptation_indices)
    adaptation_indices = torch.from_numpy(adaptation_indices)
    adaptation_data, adaptation_labels = data[adaptation_indices], labels[adaptation_indices]
    evaluation_data, evaluation_labels = data[evaluation_indices], labels[evaluation_indices]

    # Adapt the model
    for step in range(adaptation_steps):
        train_error = loss(learner(adaptation_data), adaptation_labels)
        train_error /= len(adaptation_data)
        learner.adapt(train_error)

    # Evaluate the adapted model
    predictions = learner(evaluation_data)
    valid_error = loss(predictions, evaluation_labels)
    valid_error /= len(evaluation_data)
    valid_accuracy = accuracy(predictions, evaluation_labels)
    return valid_error, valid_accuracy


def main(
        ways=5,
        shots=5,
        meta_lr=0.003,
        fast_lr=0.5,
        meta_batch_size=32,
        adaptation_steps=1,
        num_iterations=60000,
        cuda=True,
        seed=42,
):
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    device = torch.device('cpu')
    if cuda and torch.cuda.device_count():
        torch.cuda.manual_seed(seed)
        device = torch.device('cuda')


    # Create Datasets
    train_dataset = l2l.vision.datasets.FC100(root='~/data',
                                              transform=tv.transforms.ToTensor(),
                                              mode='train')
    valid_dataset = l2l.vision.datasets.FC100(root='~/data',
                                              transform=tv.transforms.ToTensor(),
                                              mode='validation')
    test_dataset = l2l.vision.datasets.FC100(root='~/data',
                                              transform=tv.transforms.ToTensor(),
                                             mode='test')
    train_dataset = l2l.data.MetaDataset(train_dataset)
    valid_dataset = l2l.data.MetaDataset(valid_dataset)
    test_dataset = l2l.data.MetaDataset(test_dataset)

    train_transforms = [
        FusedNWaysKShots(train_dataset, n=ways, k=2*shots),
        LoadData(train_dataset),
        RemapLabels(train_dataset),
        ConsecutiveLabels(train_dataset),
    ]
    train_tasks = l2l.data.TaskDataset(train_dataset,
                                       task_transforms=train_transforms,
                                       num_tasks=20000)

    valid_transforms = [
        FusedNWaysKShots(valid_dataset, n=ways, k=2*shots),
        LoadData(valid_dataset),
        ConsecutiveLabels(valid_dataset),
        RemapLabels(valid_dataset),
    ]
    valid_tasks = l2l.data.TaskDataset(valid_dataset,
                                       task_transforms=valid_transforms,
                                       num_tasks=600)

    test_transforms = [
        FusedNWaysKShots(test_dataset, n=ways, k=2*shots),
        LoadData(test_dataset),
        RemapLabels(test_dataset),
        ConsecutiveLabels(test_dataset),
    ]
    test_tasks = l2l.data.TaskDataset(test_dataset,
                                      task_transforms=test_transforms,
                                      num_tasks=600)


    # Create model
    features = l2l.vision.models.ConvBase(output_size=64, channels=3, max_pool=True)
    model = torch.nn.Sequential(features, Lambda(lambda x: x.view(-1, 256)),torch.nn.Linear(256, ways))
    model.to(device)
    maml = l2l.algorithms.MAML(model, lr=fast_lr, first_order=False)
    opt = optim.Adam(maml.parameters(), meta_lr)
    loss = nn.CrossEntropyLoss(reduction='mean')
    
    training_accuracy =  torch.ones(num_iterations)
    test_accuracy =  torch.ones(num_iterations)
    running_time = np.ones(num_iterations)
    import time
    start_time = time.time()

    for iteration in range(num_iterations):
        opt.zero_grad()
        meta_train_error = 0.0
        meta_train_accuracy = 0.0
        meta_valid_error = 0.0
        meta_valid_accuracy = 0.0
        meta_test_error = 0.0
        meta_test_accuracy = 0.0
        for task in range(meta_batch_size):
            # Compute meta-training loss
            learner = maml.clone()
            batch = train_tasks.sample()
            evaluation_error, evaluation_accuracy = fast_adapt(batch,
                                                               learner,
                                                               loss,
                                                               adaptation_steps,
                                                               shots,
                                                               ways,
                                                               device)
            evaluation_error.backward()
            meta_train_error += evaluation_error.item()
            meta_train_accuracy += evaluation_accuracy.item()

            # Compute meta-validation loss
            learner = maml.clone()
            batch = valid_tasks.sample()
            evaluation_error, evaluation_accuracy = fast_adapt(batch,
                                                               learner,
                                                               loss,
                                                               adaptation_steps,
                                                               shots,
                                                               ways,
                                                               device)
            meta_valid_error += evaluation_error.item()
            meta_valid_accuracy += evaluation_accuracy.item()
            
            # Compute meta-test loss
            learner = maml.clone()
            batch = test_tasks.sample()
            evaluation_error, evaluation_accuracy = fast_adapt(batch,
                                                               learner,
                                                               loss,
                                                               adaptation_steps,
                                                               shots,
                                                               ways,
                                                               device)
            meta_test_error += evaluation_error.item()
            meta_test_accuracy += evaluation_accuracy.item()
        
        training_accuracy[iteration] = meta_train_accuracy / meta_batch_size
        test_accuracy[iteration] = meta_test_accuracy / meta_batch_size
        
        # Print some metrics
        print('\n')
        print('Iteration', iteration)
        print('Meta Train Error', meta_train_error / meta_batch_size)
        print('Meta Train Accuracy', meta_train_accuracy / meta_batch_size)
        print('Meta Valid Error', meta_valid_error / meta_batch_size)
        print('Meta Valid Accuracy', meta_valid_accuracy / meta_batch_size)
        print('Meta Test Error', meta_test_error / meta_batch_size)
        print('Meta Test Accuracy', meta_test_accuracy / meta_batch_size)

        # Average the accumulated gradients and optimize
        for p in maml.parameters():
            p.grad.data.mul_(1.0 / meta_batch_size)
        opt.step()
        
        end_time = time.time()
        running_time[iteration] = end_time - start_time
        print('total running time', end_time - start_time)

    # meta_test_error = 0.0
    # meta_test_accuracy = 0.0
    # for task in range(meta_batch_size):
    #     # Compute meta-testing loss
    #     learner = maml.clone()
    #     batch = test_tasks.sample()
    #     evaluation_error, evaluation_accuracy = fast_adapt(batch,
    #                                                       learner,
    #                                                       loss,
    #                                                       adaptation_steps,
    #                                                       shots,
    #                                                       ways,
    #                                                       device)
    #     meta_test_error += evaluation_error.item()
    #     meta_test_accuracy += evaluation_accuracy.item()
    # print('Meta Test Error', meta_test_error / meta_batch_size)
    # print('Meta Test Accuracy', meta_test_accuracy / meta_batch_size)
    
    return training_accuracy.numpy(),test_accuracy.numpy(), running_time


if __name__ == '__main__':
    train_accuracy = []
    test_accuracy = []
    run_time = []
    
    seeds = [42,52,62,72,82]
    lr=0.001
    fastlr=0.5  # before: fastlr=0.5 & step=3 
    stp=3
    
    for seed in seeds: 
        training_accuracy,testing_accuracy, running_time = main(meta_lr=lr, 
                                                                fast_lr=fastlr, 
                                                                adaptation_steps=stp,
                                                                num_iterations=1200, 
                                                                seed=seed)
        train_accuracy.append(training_accuracy)
        test_accuracy.append(testing_accuracy)
        run_time.append(running_time)
        
    
    # save 
    # from datetime import datetime
    # now = datetime.now()
    # current_time = now.strftime("%H:%M:%S")
    
    pstr = '_lr_' + str(lr) + '_fastlr_' + str(fastlr) + '_steps_' + str(stp)
    
    with open('exp_data/train_accuracy' + pstr, 'wb') as f:
        pickle.dump(train_accuracy, f)
        
    with open('exp_data/test_accuracy' + pstr, 'wb') as f:
        pickle.dump(test_accuracy, f)
        
    with open('exp_data/run_time' + pstr, 'wb') as f:
        pickle.dump(run_time, f)

        
   
