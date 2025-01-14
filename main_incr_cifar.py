from model import IncrNet
import torch
from torch.autograd import Variable
import torchvision.transforms as transforms
import argparse
import time
import numpy as np
import cv2
import copy
import subprocess
import os
import torch.multiprocessing as mp
import atexit
import sys
import pdb

from dataset_incr_cifar import iCIFAR10, iCIFAR100

parser = argparse.ArgumentParser(description="Incremental learning")


# Saving options
parser.add_argument("--outfile", default="results/temp.csv", type=str,
                    help="Output file name (should have .csv extension)")
parser.add_argument("--save_all", dest="save_all", action="store_true",
                    help="Option to save models after each "
                         "test_freq number of learning exposures")
parser.add_argument("--save_all_dir", dest="save_all_dir", type=str,
                    default=None, help="Directory to store all models in")
parser.add_argument("--resume", dest="resume", action="store_true",
                    help="Resume training from checkpoint at outfile")
parser.add_argument("--resume_outfile", default=None, type=str,
                    help="Output file name after resuming")

# Hyperparameters
parser.add_argument("--init_lr", default=0.002, type=float,
                    help="initial learning rate")
parser.add_argument("--init_lr_ft", default=0.001, type=float,
                    help="Init learning rate for balanced finetuning (for E2E)")
parser.add_argument("--num_epoch", default=5, type=int,
                    help="Number of epochs")
parser.add_argument("--num_epoch_ft", default=10, type=int,
                    help="Number of epochs for balanced finetuning (for E2E)")
parser.add_argument("--lrd", default=5, type=float,
                    help="Learning rate decrease factor")
parser.add_argument("--wd", default=0.00001, type=float,
                    help="Weight decay for SGD")
parser.add_argument("--batch_size", default=200, type=int,
                    help="Mini batch size for training")
parser.add_argument("--llr_freq", default=10, type=int,
                    help="Learning rate lowering frequency for SGD (for E2E)")
parser.add_argument("--batch_size_test", default=200, type=int,
                    help="Mini batch size for testing")

# CRIB options
parser.add_argument("--lexp_len", default=100, type=int,
                    help="Number of frames in Learning Exposure")
parser.add_argument("--size_test", default=100, type=int,
                    help="Number of test images per object")
parser.add_argument("--num_exemplars", default=400, type=int,
                    help="number of exemplars")
parser.add_argument("--img_size", default=224, type=int,
                    help="Size of images input to the network")
parser.add_argument("--rendered_img_size", default=300, type=int,
                    help="Size of rendered images")
parser.add_argument("--total_classes", default=20, type=int,
                    help="Total number of classes")
parser.add_argument("--num_classes", default=1, type=int,
                    help="Number of classes for each learning exposure")
parser.add_argument("--num_iters", default=1000, type=int,
                    help="Total number of learning exposures (currently"
                         " only integer multiples of args.total_classes"
                         " each class seen equal number of times)")

# Model options
parser.add_argument("--algo", default="icarl", type=str,
                    help="Algorithm to run. Options : icarl, e2e, lwf")
parser.add_argument("--no_dist", dest="dist", action="store_false",
                    help="Option to switch off distillation loss")
parser.add_argument("--pt", dest="pretrained", action="store_true",
                    help="Option to start from an ImageNet pretrained model")
parser.add_argument("--ncm", dest="ncm", action="store_true",
                    help="Use nearest class mean classification (for E2E)")

# Training options
parser.add_argument("--diff_order", dest="d_order", action="store_true",
                    help="Use a random order of classes introduced")
parser.add_argument("--no_jitter", dest="jitter", action="store_false",
                    help="Option for no color jittering (for iCaRL)")
parser.add_argument("--h_ch", default=0.02, type=float,
                    help="Color jittering : max hue change")
parser.add_argument("--s_ch", default=0.05, type=float,
                    help="Color jittering : max saturation change")
parser.add_argument("--l_ch", default=0.1, type=float,
                    help="Color jittering : max lightness change")

# System options
parser.add_argument("--test_freq", default=1, type=int,
                    help="Number of iterations of training after"
                         " which a test is done/model saved")
parser.add_argument("--num_workers", default=8, type=int,
                    help="Maximum number of threads spawned at any" 
                         "stage of execution")
parser.add_argument("--one_gpu", dest="one_gpu", action="store_true",
                    help="Option to run multiprocessing on 1 GPU")


parser.set_defaults(pre_augment=False)
parser.set_defaults(ncm=False)
parser.set_defaults(dist=False)
parser.set_defaults(pretrained=True)
parser.set_defaults(d_order=False)
parser.set_defaults(jitter=True)
parser.set_defaults(save_all=False)
parser.set_defaults(resume=False)
parser.set_defaults(one_gpu=False)


# Print help if no arguments passed
if len(sys.argv)==1:
    parser.print_help(sys.stderr)
    sys.exit(1)

args = parser.parse_args()
torch.backends.cudnn.benchmark = True
mp.set_sharing_strategy("file_system")
expt_githash = subprocess.check_output(["git", "describe", "--always"])

# Defines transform
transform = transforms.ColorJitter(hue=args.h_ch, 
                                   saturation=args.s_ch, 
                                   brightness=args.l_ch)

# multiprocessing on a single GPU
if args.one_gpu:
    mp.get_context("spawn")

# GPU indices
train_device = 0
test_device = 1
if args.one_gpu:
    test_device = 0

if not os.path.exists(os.path.dirname(args.outfile)):
    if len(os.path.dirname(args.outfile)) != 0:
        os.makedirs(os.path.dirname(args.outfile))


num_classes = args.num_classes
test_freq = 1
total_classes = args.total_classes
num_iters = args.num_iters

         
# Conditional variable, shared vars for synchronization
cond_var = mp.Condition()
train_counter = mp.Value("i", 0)
test_counter = mp.Value("i", 0)
dataQueue = mp.Queue()
all_done = mp.Event()
data_mgr = mp.Manager()

if not os.path.exists("data_generator/cifar_mean_image.npy"):
    mean_image = None
else:
    mean_image = np.load("data_generator/cifar_mean_image.npy")

K = args.num_exemplars  # total number of exemplars

model = IncrNet(args, device=train_device, cifar=True)

# Randomly choose a subset of classes
perm_id = np.random.choice(100, total_classes, replace=False)

perm_id = np.random.permutation(perm_id)

print ("perm_id:", perm_id)

if args.num_iters > args.total_classes:
    if not args.num_iters % args.total_classes == 0:
        raise Exception("Currently no support for num_iters%total_classes != 0")
    
    num_repetitions = args.num_iters//args.total_classes
    perm_file = "permutation_files/permutation_%d_%d.npy" \
                                    % (args.total_classes, num_repetitions)

    if not os.path.exists(perm_file):
        os.makedirs("permutation_files", exist_ok=True)
        # Create random permutation file and save
        perm_arr = np.array(num_repetitions 
                            * list(np.arange(args.total_classes)))
        np.random.shuffle(perm_arr)
        np.save(perm_file, perm_arr)

    perm_id_all = np.load(perm_file) 
    for i in range(len(perm_id_all)):
        perm_id_all[i] = perm_id[perm_id_all[i]]
    perm_id = perm_id_all

train_set = iCIFAR100(args, root="./data",
                             train=True,
                             classes=perm_id,
                             download=True,
                             transform=transform,
                             mean_image=mean_image)
test_set = iCIFAR100(args, root="./data",
                             train=False,
                             classes=perm_id,
                             download=True,
                             transform=None,
                             mean_image=mean_image)

acc_matr = np.zeros((int(total_classes/num_classes), num_iters))
coverage = np.zeros((int(total_classes/num_classes), num_iters))
n_known = np.zeros(num_iters, dtype=np.int32)

classes_seen = []
model_classes_seen = []  # Class index numbers stored by model
exemplar_data = []  # Exemplar set information stored by the model
# acc_matr row index represents class number and column index represents
# learning exposure.

# Conditional variable, shared memory for synchronization
cond_var = mp.Condition()
train_counter = mp.Value("i", 0)
test_counter = mp.Value("i", 0)
dataQueue = mp.Queue()
all_done = mp.Event()
data_mgr = mp.Manager()
expanded_classes = data_mgr.list([None for i in range(args.test_freq)])

if args.resume:
    print("resuming model from %s-model.pth.tar" %
          os.path.splitext(args.outfile)[0])

    model = torch.load("%s-model.pth.tar" % os.path.splitext(args.outfile)[0], 
                       map_location=lambda storage, loc: storage)
    model.device = train_device

    model.exemplar_means = []
    model.compute_means = True

    info_coverage = np.load("%s-coverage.npz" \
        % os.path.splitext(args.outfile)[0])
    info_matr = np.load("%s-matr.npz" % os.path.splitext(args.outfile)[0])
    if expt_githash != info_coverage["expt_githash"]:
        print("Warning : Code was changed since the last time model was saved")
        print("Last commit hash : ", info_coverage["expt_githash"])
        print("Current commit hash : ", expt_githash)

    args_resume_outfile = args.resume_outfile
    perm_id = info_coverage["perm_id"]
    num_iters_done = model.num_iters_done
    acc_matr = info_matr["acc_matr"]
    args = info_matr["args"].item()

    if args_resume_outfile is not None:
        args.outfile = args.resume_outfile = args_resume_outfile
    else:
        print("Overwriting old files")

    model_classes_seen = list(
        info_coverage["model_classes_seen"][:num_iters_done])
    classes_seen = list(info_coverage["classes_seen"][:num_iters_done])
    coverage = info_coverage["coverage"]
    train_set.all_train_coverage = info_coverage["train_coverage"]

    train_counter = mp.Value("i", num_iters_done)
    test_counter = mp.Value("i", num_iters_done)

    # expanding test set to everything seen earlier
    for mdl_cl, gt_cl in zip(model_classes_seen, classes_seen):
        print("Expanding class for resuming : %d, %d" %(mdl_cl, gt_cl))
        test_set.expand([mdl_cl], [gt_cl])

    # Ensuring requires_grad = True after model reload
    for p in model.parameters():
        p.requires_grad = True


def train_run(device):
    global train_set
    model.cuda(device=device)

    train_wait_time = 0
    s = len(classes_seen)
    print("####### Train Process Running ########")
    print("Args: ", args)
    train_wait_time = 0

    while s < args.num_iters:
        time_ptr = time.time()
        # Do not start training till test process catches up
        cond_var.acquire()
        # while loop to avoid spurious wakeups
        while test_counter.value + args.test_freq <= train_counter.value:
            print("[Train Process] Waiting on test process")
            print("[Train Process] train_counter : ", train_counter.value)
            print("[Train Process] test_counter : ", test_counter.value)
            cond_var.wait()
        cond_var.release()
        train_wait_time += time.time() - time_ptr

        # Keep a copy of previous model for distillation
        prev_model = copy.deepcopy(model)
        prev_model.cuda(device=device)
        for p in prev_model.parameters():
            p.requires_grad = False

        curr_class = perm_id[s]

        classes_seen.append(curr_class)
        curr_expanded = False

        if curr_class not in model.classes_map:
            model.increment_classes([curr_class])
            model.cuda(device=device)
            curr_expanded = True

        model_curr_class_idx = model.classes_map[curr_class]
        model_classes_seen.append(model_curr_class_idx)

        # Load Datasets
        print("Loading training examples for"\
              " class index %d , %s, at iteration %d" % 
              (model_curr_class_idx, curr_class, s))
        train_set.load_data_class([curr_class], [model_curr_class_idx], s)

        model.train()

        model.update_representation_icarl(train_set, \
                                          prev_model, \
                                          [model_curr_class_idx], \
                                          args.num_workers)
        model.eval()
        del prev_model
        m = int(K / model.n_classes)

        model.reduce_exemplar_sets(m)
        # Construct exemplar sets for current class
        print("Constructing exemplar set for class index %d , %s ..." \
            %(model_curr_class_idx, curr_class), end="")
        images, le_maps = train_set.get_image_class(
                model_curr_class_idx)
        mean_images = np.array([mean_image]*len(images))
        model.construct_exemplar_set(images, \
                                     mean_images, \
                                     le_maps, None, m, \
                                     model_curr_class_idx, s)
        model.n_known = model.n_classes

        n_known[s] = model.n_known
        print("Model num classes : %d, " % model.n_known)
        
        if s > 0:
            coverage[:,s] = coverage[:,s-1]
        coverage[model_curr_class_idx,s] = \
                            train_set.get_train_coverage(perm_id[s])
        print("Coverage of current class now: ", \
                    coverage[model_curr_class_idx,s])

        for y, P_y in enumerate(model.exemplar_sets):
            print("Exemplar set for class-%d:" % (y), P_y.shape)

        exemplar_data.append(list(model.eset_le_maps))


        cond_var.acquire()
        train_counter.value += 1
        if curr_expanded:
            expanded_classes[s % args.test_freq] = model_curr_class_idx, curr_class
        else:
            expanded_classes[s % args.test_freq] = None

        if train_counter.value == test_counter.value + args.test_freq:
            temp_model = copy.deepcopy(model)
            temp_model.cpu()
            dataQueue.put(temp_model)
        cond_var.notify_all()
        cond_var.release()

            
            
        np.savez(args.outfile[:-4] + "-coverage.npz", \
                 coverage=coverage, \
                 perm_id=perm_id, n_known=n_known, \
                 train_coverage=train_set.all_train_coverage, \
                 model_classes_seen=model_classes_seen, \
                 classes_seen=classes_seen, \
                 expt_githash=expt_githash, \
                 exemplar_data=np.array(exemplar_data))
        # loop var increment
        s += 1
    
    time_ptr = time.time()
    all_done.wait()
    train_wait_time += time.time() - time_ptr
    print("[Train Process] Done, total time spent waiting : ", train_wait_time)

def test_run(device):
    global test_set
    print("####### Test Process Running ########")
    test_model = None
    s = args.test_freq * (len(classes_seen)//args.test_freq)

    test_wait_time = 0
    with open(args.outfile, "w") as file:
        print("Model classes, Test Accuracy", file=file)
        while s < args.num_iters:

            # Wait till training is done
            time_ptr = time.time()
            cond_var.acquire()
            while train_counter.value < test_counter.value + args.test_freq:
                print("[Test Process] Waiting on train process")
                print("[Test Process] train_counter : ", train_counter.value)
                print("[Test Process] test_counter : ", test_counter.value)
                cond_var.wait()
            cond_var.release()
            test_wait_time += time.time() - time_ptr

            cond_var.acquire()
            test_model = dataQueue.get()
            expanded_classes_copy = copy.deepcopy(expanded_classes)
            test_counter.value += args.test_freq
            cond_var.notify_all()
            cond_var.release()


    
            # test set only needs to be expanded
            # when a new exposure is seen
            for expanded_class in expanded_classes_copy:
                if expanded_class is not None:
                    model_cl, gt_cl = expanded_class
                    print("[Test Process] Loading test data")
                    test_set.expand([model_cl], [gt_cl])

            print("[Test Process] Test Set Length:", len(test_set))
            

            test_model.device = device
            test_model.cuda(device=device)
            test_model.eval()
            test_loader = torch.utils.data.DataLoader(test_set, 
                batch_size=args.batch_size_test, shuffle=False, \
                num_workers=args.num_workers, pin_memory=True)

            print("%d, " % test_model.n_known, end="", file=file)

            print("[Test Process] Computing Accuracy matrix...")
            all_labels = []
            all_preds = []
            with torch.no_grad():
                for indices, images, labels in test_loader:
                    images = Variable(images).cuda(device=device)
                    preds = test_model.classify(images, 
                                                mean_image, 
                                                args.img_size)
                    all_preds.append(preds.data.cpu().numpy())
                    all_labels.append(labels.numpy())
            all_preds = np.concatenate(all_preds, axis=0)
            all_labels = np.concatenate(all_labels, axis=0)

            for i in range(test_model.n_known):
                class_preds = all_preds[all_labels == i]
                correct = np.sum(class_preds == i)
                total = len(class_preds)
                acc_matr[i, s] = (100.0 * correct/total)

            test_acc = np.mean(acc_matr[:test_model.n_known, s])
            print("%.2f ," % test_acc, file=file)
            print("[Test Process] =======> Test Accuracy after %d"
                  " learning exposures : " %
                  (s + args.test_freq), test_acc)

            print("[Test Process] Saving model and other data")
            test_model.cpu()
            test_model.num_iters_done = s + args.test_freq
            if not args.save_all:
                torch.save(test_model, "%s-model.pth.tar" %
                           os.path.splitext(args.outfile)[0])
            else:
                torch.save(test_model, "%s-saved_models/model_iter_%d.pth.tar"\
                                        %(os.path.join(args.save_all_dir, \
                                        os.path.splitext(args.outfile)[0]), s))

            # loop var increment
            s += args.test_freq

            np.savez(args.outfile[:-4] + "-matr.npz", acc_matr=acc_matr, \
                     hyper_params = test_model.fetch_hyper_params(), \
                     githash=expt_githash, args=args, num_iter_done=s)
            
        print("[Test Process] Done, total time spent waiting : ", 
              test_wait_time)
        all_done.set()

def cleanup(train_process, test_process):
    train_process.terminate()
    test_process.terminate()


def main():
    train_process = mp.Process(target=train_run, args=(train_device,))
    test_process = mp.Process(target=test_run, args=(test_device,))
    atexit.register(cleanup, train_process, test_process)
    train_process.start()
    test_process.start()

    train_process.join()
    print("Train Process Completed")
    test_process.join()
    print("Test Process Completed")


if __name__ == "__main__":
    main()
