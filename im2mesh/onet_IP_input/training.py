import os
from tqdm import trange
import torch
from torch.nn import functional as F
from torch import distributions as dist
from im2mesh.common import (
    compute_iou, make_3d_grid
)
from im2mesh.utils import visualize as vis
from im2mesh.training import BaseTrainer
import im2mesh.common as common
# from im2mesh.common import (
#     Projection2d)


class Trainer(BaseTrainer):
    ''' Trainer object for the Occupancy Network.

    Args:
        model (nn.Module): Occupancy Network model
        optimizer (optimizer): pytorch optimizer object
        device (device): pytorch device
        input_type (str): input type
        vis_dir (str): visualization directory
        threshold (float): threshold value
        eval_sample (bool): whether to evaluate samples

    '''

    def __init__(self, model, optimizer, device=None, input_type='img',
                 vis_dir=None, threshold=0.5, eval_sample=False, beta_vae=1.):
                 
        self.model = model
        self.optimizer = optimizer
        self.device = device
        self.input_type = input_type
        self.vis_dir = vis_dir
        self.threshold = threshold
        self.eval_sample = eval_sample
        self.beta_vae = beta_vae

        # self.p2d = Projection2d()
        if vis_dir is not None and not os.path.exists(vis_dir):
            os.makedirs(vis_dir)

    def train_step(self, cfg, data, DEGREES = 0):
        ''' Performs a training step.

        Args:
            data (dict): data dictionary
        '''
        self.model.train()
        self.optimizer.zero_grad()
        loss = self.compute_loss(cfg, data, DEGREES = DEGREES)
        loss.backward()
        self.optimizer.step()
        return loss.item()

    def eval_step(self, data):
        ''' Performs an evaluation step.

        Args:
            data (dict): data dictionary
        '''
        self.model.eval()

        device = self.device
        threshold = self.threshold
        eval_dict = {}

        # Compute elbo
        points = data.get('points').to(device)
        occ = data.get('points.occ').to(device)

        inputs = data.get('inputs', torch.empty(points.size(0), 0)).to(device)
        voxels_occ = data.get('voxels')

        points_iou = data.get('points_iou').to(device)
        occ_iou = data.get('points_iou.occ').to(device)

        world_mat = data.get('inputs.world_mat').to(device)
        camera_mat = data.get('inputs.camera_mat').to(device)
        camera_args = common.get_camera_args(
            data, 'points.loc', 'points.scale', device=self.device)
        world_mat, camera_mat = camera_args['Rt'], camera_args['K']

        kwargs = {}

        with torch.no_grad():
            elbo, rec_error, kl = self.model.compute_elbo(
                points, occ, inputs,world_mat, camera_mat, **kwargs)

        eval_dict['loss'] = -elbo.mean().item()
        eval_dict['rec_error'] = rec_error.mean().item()
        eval_dict['kl'] = kl.mean().item()

        # Compute iou
        batch_size = points.size(0)

        with torch.no_grad():
            p_out = self.model(points_iou, inputs, world_mat, camera_mat,
                               sample=self.eval_sample, **kwargs)

        occ_iou_np = (occ_iou >= 0.5).cpu().numpy()
        occ_iou_hat_np = (p_out.probs >= threshold).cpu().numpy()
        iou = compute_iou(occ_iou_np, occ_iou_hat_np).mean()
        eval_dict['iou'] = iou

        # Estimate voxel iou
        if voxels_occ is not None:
            voxels_occ = voxels_occ.to(device)
            points_voxels = make_3d_grid(
                (-0.5 + 1/64,) * 3, (0.5 - 1/64,) * 3, (32,) * 3)
            points_voxels = points_voxels.expand(
                batch_size, *points_voxels.size())
            points_voxels = points_voxels.to(device)
            with torch.no_grad():
                p_out = self.model(points_voxels, inputs, world_mat, camera_mat,
                                   sample=self.eval_sample, **kwargs)

            voxels_occ_np = (voxels_occ >= 0.5).cpu().numpy()
            occ_hat_np = (p_out.probs >= threshold).cpu().numpy()
            iou_voxels = compute_iou(voxels_occ_np, occ_hat_np).mean()

            eval_dict['iou_voxels'] = iou_voxels

        return eval_dict

    def visualize(self, data):
        ''' Performs a visualization step for the data.

        Args:
            data (dict): data dictionary
        '''
        device = self.device

        batch_size = data['points'].size(0)
        inputs = data.get('inputs', torch.empty(batch_size, 0)).to(device)

        world_mat = data.get('inputs.world_mat').to(device)
        camera_mat = data.get('inputs.camera_mat').to(device)
        camera_args = common.get_camera_args(
            data, 'points.loc', 'points.scale', device=self.device)
        world_mat, camera_mat = camera_args['Rt'], camera_args['K']

        shape = (32, 32, 32)
        p = make_3d_grid([-0.5] * 3, [0.5] * 3, shape).to(device)
        p = p.expand(batch_size, *p.size())

        kwargs = {}
        with torch.no_grad():
            p_r = self.model(p, inputs, world_mat, camera_mat, sample=self.eval_sample, **kwargs)

        occ_hat = p_r.probs.view(batch_size, *shape)
        voxels_out = (occ_hat >= self.threshold).cpu().numpy()

        for i in trange(batch_size):
            input_img_path = os.path.join(self.vis_dir, '%03d_in.png' % i)
            vis.visualize_data(
                inputs[i].cpu(), self.input_type, input_img_path)
            vis.visualize_voxels(
                voxels_out[i], os.path.join(self.vis_dir, '%03d.png' % i))

    def triplet_loss_normals(self, normals):
        """Calculates triplet loss of cos similarities among normals
           max(abs(cos_similarity), 0)
           where cos_similarity = sum[cos(normal_{i}, normal_{j})]
                                  for i in [o, num_normals], j in [i, num_normals]
        Args:
            normals ([ torch.tensor]): normals shape [Batch, Num_normals, 3]
        Returns:
            [torch.tensor]: losses: 
                shape [Batch, C_{Num_normals}^{2}]
                C_{Num_normals}^{2} - binom coefficient (number of combination of 2 from Num_normals elements)
        """
        batch, T, dim = normals.size()
        similarity_list = []
        for i in range(T):
            for j in range(i + 1, T):
                similarity = torch.pow(torch.cosine_similarity(normals[:, i, :], normals[:, j, :]), 10).unsqueeze(0)
                similarity_list.append(similarity)
        sim_tensor = torch.cat(similarity_list)
        sim_tensor = torch.transpose(sim_tensor, 0, 1)
        sim_tensor = torch.sum(sim_tensor, dim=1).unsqueeze(1)
        # sim_tensor = sim_tensor.mean(-1) #in case we want average over all combinations
        zeros = torch.zeros(batch).unsqueeze(1).to(self.device)
        losses = torch.max(sim_tensor, zeros)
        return losses

    def compute_loss(self, data):
        ''' Computes the loss.

        Args:
            data (dict): data dictionary
        '''
        device = self.device
        p = data.get('points').to(device)
        occ = data.get('points.occ').to(device)
        inputs = data.get('inputs', torch.empty(p.size(0), 0)).to(device)
        I = data.get('inputs.image', torch.empty(p.size(0), 0)).to(device)
        
        world_mat = data.get('inputs.world_mat').to(device)
        camera_mat = data.get('inputs.camera_mat').to(device)
        camera_args = common.get_camera_args(
            data, 'points.loc', 'points.scale', device=self.device)
        world_mat, camera_mat = camera_args['Rt'], camera_args['K']
        
        # print("world_mat",world_mat.shape)
        # print("camera_mat",camera_mat.shape)
        # exit(1)

        kwargs = {}











        c = self.model.encode_inputs(inputs)
        # print("c",c[0].shape)
        # print("c",c[1].shape)
        # print("c",c[2].shape)
        # print("c",c[3].shape)
        v = self.model.gproj(p, c, world_mat, camera_mat, inputs, False)
        # v = self.model.gproj(p, c, camera_mat, inputs, True)
        
        q_z = self.model.infer_z(p, occ, c, **kwargs)
        z = q_z.rsample()

        # KL-divergence
        kl = dist.kl_divergence(q_z, self.model.p0_z).sum(dim=-1)
        loss = kl.mean()

        # General points
        logits = self.model.decode(p, z, v, **kwargs).logits
        
        # exit(1)
        
        loss_i = F.binary_cross_entropy_with_logits(
            logits, occ, reduction='none')
        loss = loss + loss_i.sum(-1).mean()

        return loss

    def rotate_points(self, pointcloud_model, DEGREES = 0, query_points = False, use_rotation_tensor = False, save_rotation_tensor = False):
            ## https://en.wikipedia.org/wiki/Rotation_matrix
            ## https://github.com/dsvilarkovic/dynamic_plane_convolutional_onet/
        """
            Function for rotating points
            Args:
                pointcloud_model (numpy 3d array) - batch_size x pointcloud_size x 3d channel sized numpy array which presents pointcloud
                DEGREES (int) - range of rotations to be used
                query_points (boolean) - used for rotating query points with already existing rotation matrix
                use_rotation_tensor (boolean) - asking whether DEGREES should be used for generating new rotation matrix, or use the already established one
                save_rotation_tensor (boolean) - asking to keep rotation matrix in a pytorch .pt file
        """
        if(use_rotation_tensor != True):
            angle_range = DEGREES
            x_angle = radians(random.random() * angle_range)
            y_angle = radians(random.random() * angle_range)
            z_angle = radians(random.random() * angle_range)

            rot_x = torch.Tensor([[1,0,0,0],[0, cos(x_angle),-sin(x_angle),0], [0, sin(x_angle), cos(x_angle),0], [0,0,0,1]])
            rot_y = torch.Tensor([[cos(y_angle),0,sin(y_angle), 0],[0, 1, 0,0], [-sin(y_angle),0,cos(y_angle),0], [0,0,0,1]])
            rot_z = torch.Tensor([[cos(z_angle), -sin(z_angle),0,0],[sin(z_angle), cos(z_angle),0,0],[0,0,1,0], [0,0,0,1]])
            rotation_matrix = torch.mm(rot_y, rot_z)
            rotation_matrix = torch.mm(rot_x,rotation_matrix)        

            batch_size, point_cloud_size, _ = pointcloud_model.shape
            pointcloud_model = torch.cat([pointcloud_model, torch.ones(batch_size, point_cloud_size,1).to(self.device)], dim = 2)
            
    
            pointcloud_model_rotated = torch.matmul(pointcloud_model, rotation_matrix.to(self.device))
            self.rotation_matrix = rotation_matrix
            
            if(save_rotation_tensor):
                torch.save(rotation_matrix, 'rotation_matrix.pt') #used for plane prediction, change it at your will 
            return pointcloud_model_rotated[:,:,0:3], (x_angle, y_angle, z_angle)
        else: 
            batch_size, point_cloud_size, _ = pointcloud_model.shape
            pointcloud_model = pointcloud_model / sqrt(0.55**2 + 0.55**2 + 0.55**2)
            pointcloud_model = torch.cat([pointcloud_model, torch.ones(batch_size, point_cloud_size,1).to(self.device)], dim = 2)
            pointcloud_model_rotated =torch.matmul(pointcloud_model, self.rotation_matrix.to(self.device))
            return pointcloud_model_rotated[:,:,0:3]