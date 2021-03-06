import pdb
import sys
import argparse
import numpy as np
from tqdm import tqdm
from PIL import Image
from io import BytesIO
import matplotlib
matplotlib.use('Agg')
from matplotlib import pyplot as plt

import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.backends.cudnn as cudnn
from torch.autograd import Variable
from torch.optim.lr_scheduler import MultiStepLR
from torchvision import datasets, transforms

import seaborn as sns  # import this after torch or it will break everything

from models.vgg import VGG
from models.densenet import DenseNet3
from models.wideresnet import WideResNet
from utils.utils import encode_onehot, CSVLogger, Cutout

from tensorboardX import SummaryWriter

conf_histogram = None

dataset_options = ['cifar10', 'svhn']
model_options = ['wideresnet', 'densenet', 'vgg13']

parser = argparse.ArgumentParser(description='CNN')
parser.add_argument('--dataset', default='cifar10', choices=dataset_options)
parser.add_argument('--model', default='vgg13', choices=model_options)
parser.add_argument('--batch_size', type=int, default=128)
parser.add_argument('--epochs', type=int, default=200)
parser.add_argument('--seed', type=int, default=0)
parser.add_argument('--learning_rate', type=float, default=0.1)
parser.add_argument('--data_augmentation', action='store_true', default=False,
                    help='augment data by flipping and cropping')
parser.add_argument('--cutout', type=int, default=16, metavar='S',
                    help='patch size to cut out. 0 indicates no cutout')
parser.add_argument('--budget', type=float, default=0.3, metavar='N',
                    help='the budget for how often the network can get hints')
parser.add_argument('--baseline', action='store_true', default=False,
                    help='train model without confidence branch')
parser.add_argument('--log_dir', type=str, help='dir to save event files')
parser.add_argument('--tensorboard', action='store_true', default=False,
                    help='write results to events file')
parser.add_argument('--device', type=int, default=-1, help='use cpu or gpu')
parser.add_argument('--subsample', action='store_true', default=False,
                    help='subsample train and test dataset')

args = parser.parse_args()
cudnn.benchmark = True  # Should make training go faster for large models
cudnn.benchmark = True  # Should make training go faster for large models

if args.device >= 0 and torch.cuda.is_available():
    device = torch.device('cuda:' + str(args.device))
else:
    device = torch.device('cpu')

if args.baseline:
    args.budget = 0.

filename = args.dataset + '_' + args.model + '_budget_' + str(args.budget) + '_seed_' + str(args.seed)

if args.dataset == 'svhn' and args.model == 'wideresnet':
        args.model = 'wideresnet16_8'

np.random.seed(0)
torch.cuda.manual_seed(args.seed)

print(args)


# initialize file writer
if args.tensorboard:
    if args.log_dir:
        writer = SummaryWriter(log_dir=args.log_dir)
    else:
        print("please provide a directory to save event files: '--log_dir'")
        sys.exit(1)
else:
    writer = None


# Image Preprocessing
if args.dataset == 'svhn':
    normalize = transforms.Normalize(mean=[x / 255.0 for x in[109.9, 109.7, 113.8]],
                                     std=[x / 255.0 for x in [50.1, 50.6, 50.8]])
else:
    normalize = transforms.Normalize(mean=[x / 255.0 for x in [125.3, 123.0, 113.9]],
                                     std=[x / 255.0 for x in [63.0, 62.1, 66.7]])

train_transform = transforms.Compose([])
if args.data_augmentation:
    train_transform.transforms.append(transforms.RandomCrop(32, padding=4))
    if args.dataset != 'svhn':
        train_transform.transforms.append(transforms.RandomHorizontalFlip())
train_transform.transforms.append(transforms.ToTensor())
train_transform.transforms.append(normalize)
if args.cutout > 0:
    train_transform.transforms.append(Cutout(args.cutout))

test_transform = transforms.Compose([
    transforms.ToTensor(),
    normalize])

if args.dataset == 'cifar10':
    num_classes = 10
    train_dataset = datasets.CIFAR10(root='data/',
                                     train=True,
                                     transform=train_transform,
                                     download=True)

    test_dataset = datasets.CIFAR10(root='data/',
                                    train=False,
                                    transform=test_transform,
                                    download=True)

elif args.dataset == 'svhn':
    num_classes = 10
    train_dataset = datasets.SVHN(root='data/',
                                  split='train',
                                  transform=train_transform,
                                  download=True)

    test_dataset = datasets.SVHN(root='data/',
                                 split='test',
                                 transform=test_transform,
                                 download=True)

if args.subsample:
    train_size = train_dataset.train_data.shape[0]
    train_sampler = torch.utils.data.sampler.SubsetRandomSampler(range(int(train_size/100)))
    test_size = test_dataset.test_data.shape[0]
    test_sampler = torch.utils.data.sampler.SubsetRandomSampler(range(int(test_size/100)))
else:
    train_sampler = None
    test_sampler = None

# Data Loader (Input Pipeline)
train_loader = torch.utils.data.DataLoader(dataset=train_dataset,
                                           batch_size=args.batch_size,
                                           shuffle=False,
                                           pin_memory=True,
                                           num_workers=2,
                                           sampler=train_sampler)

test_loader = torch.utils.data.DataLoader(dataset=test_dataset,
                                          batch_size=args.batch_size,
                                          shuffle=False,
                                          pin_memory=True,
                                          num_workers=2,
                                          sampler=test_sampler)


def plot_histograms(corr, conf, bins=50, norm_hist=True, epoch=None):
    # Plot histogram of correctly classified and misclassified examples in tensorboard
    global conf_histogram

    plt.figure(figsize=(6, 4))
    sns.distplot(conf[corr], kde=False, bins=bins, norm_hist=norm_hist, label='Correct')
    sns.distplot(conf[np.invert(corr)], kde=False, bins=bins, norm_hist=norm_hist, label='Incorrect')
    plt.xlabel('Confidence')
    plt.ylabel('Density')
    plt.legend()

    # the image buffer acts as if it where a location on disk
    img_buffer = BytesIO()
    plt.savefig(img_buffer, bbox_inches='tight', pad_inches=0)
    img = Image.open(img_buffer)
    img = img.convert('RGB')
    img = torch.FloatTensor(np.array(img)).permute(2, 0, 1)
    if writer is not None:
        conf_histogram = writer.add_image('confidence histogram', img, epoch)


def test(loader, epoch=None):
    cnn.eval()    # Change model to 'eval' mode (BN uses moving mean/var).

    correct = []
    probability = []
    confidence = []


    for images, labels in loader:
        with torch.no_grad():
            images = images.to(device)
        labels = labels.to(device)

        pred, conf = cnn(images)
        pred = F.softmax(pred, dim=-1)
        conf = torch.sigmoid(conf).data.view(-1)

        pred_value, pred = torch.max(pred.data, 1)
        correct.extend((pred == labels).cpu().numpy())
        probability.extend(pred_value.cpu().numpy())
        confidence.extend(conf.cpu().numpy())

    correct = np.array(correct).astype(bool)
    probability = np.array(probability)
    confidence = np.array(confidence)

    if args.baseline:
        plot_histograms(correct, probability, epoch=epoch)
    else:
        plot_histograms(correct, confidence, epoch=epoch)

    val_acc = np.mean(correct)
    conf_min = np.min(confidence)
    conf_max = np.max(confidence)
    conf_avg = np.mean(confidence)

    cnn.train()
    return val_acc, conf_min, conf_max, conf_avg


if args.model == 'wideresnet':
    cnn = WideResNet(depth=28, num_classes=num_classes, widen_factor=10).to(device)
elif args.model == 'wideresnet16_8':
    cnn = WideResNet(depth=16, num_classes=num_classes, widen_factor=8).to(device)
elif args.model == 'densenet':
    cnn = DenseNet3(depth=100, num_classes=num_classes, growth_rate=12, reduction=0.5).to(device)
elif args.model == 'vgg13':
    cnn = VGG(vgg_name='VGG13', num_classes=num_classes).to(device)

prediction_criterion = nn.NLLLoss().to(device)

cnn_optimizer = torch.optim.SGD(cnn.parameters(), lr=args.learning_rate,
                                momentum=0.9, nesterov=True, weight_decay=5e-4)

if args.dataset == 'svhn':
    scheduler = MultiStepLR(cnn_optimizer, milestones=[80, 120], gamma=0.1)
else:
    scheduler = MultiStepLR(cnn_optimizer, milestones=[60, 120, 160], gamma=0.2)

if args.model == 'densenet':
    cnn_optimizer = torch.optim.SGD(cnn.parameters(), lr=args.learning_rate,
                                    momentum=0.9, nesterov=True, weight_decay=1e-4)
    scheduler = MultiStepLR(cnn_optimizer, milestones=[150, 225], gamma=0.1)


csv_logger = CSVLogger(args=args, filename='logs/' + filename + '.csv',
                       fieldnames=['epoch', 'train_acc', 'test_acc'])

# Start with a reasonable guess for lambda
lmbda = 0.1


for epoch in range(args.epochs):

    xentropy_loss_avg = 0.
    confidence_loss_avg = 0.
    correct_count = 0.
    total = 0.

    progress_bar = tqdm(train_loader)
    for i, (images, labels) in enumerate(progress_bar):
        progress_bar.set_description('Epoch ' + str(epoch))

        images = images.to(device)
        labels = labels.to(device)
        labels_onehot = encode_onehot(labels, num_classes)

        cnn.zero_grad()

        pred_original, confidence = cnn(images)

        pred_original = F.softmax(pred_original, dim=-1)
        confidence = torch.sigmoid(confidence)

        # Make sure we don't have any numerical instability
        eps = 1e-12
        pred_original = torch.clamp(pred_original, 0. + eps, 1. - eps)
        confidence = torch.clamp(confidence, 0. + eps, 1. - eps)

        if not args.baseline:
            # Randomly set half of the confidences to 1 (i.e. no hints)
            b = Variable(torch.bernoulli(torch.Tensor(confidence.size()).uniform_(0, 1))).to(device)
            conf = confidence * b + (1 - b)
            pred_new = pred_original * conf.expand_as(pred_original) + labels_onehot * (1 - conf.expand_as(labels_onehot))
            pred_new = torch.log(pred_new)
        else:
            pred_new = torch.log(pred_original)

        xentropy_loss = prediction_criterion(pred_new, labels)
        confidence_loss = torch.mean(-torch.log(confidence))

        if args.baseline:
            total_loss = xentropy_loss
        else:
            total_loss = xentropy_loss + (lmbda * confidence_loss)

            if args.budget > confidence_loss.item():
                lmbda = lmbda / 1.01
            elif args.budget <= confidence_loss.item():
                lmbda = lmbda / 0.99

        total_loss.backward()
        cnn_optimizer.step()

        xentropy_loss_avg += xentropy_loss.item()
        confidence_loss_avg += confidence_loss.item()

        pred_idx = torch.max(pred_original.data, 1)[1]
        total += labels.size(0)
        correct_count += (pred_idx == labels.data).sum()
        accuracy = correct_count / total

        progress_bar.set_postfix(
            xentropy='%.3f' % (xentropy_loss_avg / (i + 1)),
            confidence_loss='%.3f' % (confidence_loss_avg / (i + 1)),
            acc='%.3f' % accuracy)

        if writer is not None:
            writer.add_scalars(f'data/scalar_group', {'xentropy_loss': xentropy_loss_avg / (i + 1),
                                                      'confidence_loss': confidence_loss_avg / (i + 1),
                                                      'lambd': lmbda,
                                                      }, i)

    test_acc, conf_min, conf_max, conf_avg = test(test_loader, epoch=epoch)
    tqdm.write('test_acc: %.3f, conf_min: %.3f, conf_max: %.3f, conf_avg: %.3f' % (test_acc, conf_min, conf_max, conf_avg))



    scheduler.step(epoch)

    row = {'epoch': str(epoch), 'train_acc': str(accuracy), 'test_acc': str(test_acc)}
    csv_logger.writerow(row)

    torch.save(cnn.state_dict(), 'checkpoints/' + filename + '.pt')

csv_logger.close()
