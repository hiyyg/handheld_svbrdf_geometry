from abc import ABC, abstractmethod
import torch

from parametrizations.parametrization import Parametrization

from utils.depth_maps import depth_map_to_locations
from utils.logging import error
from utils.vectors import inner_product, cross_product, normalize, norm

class LocationParametrization(Parametrization):
    @abstractmethod
    def initialize(self, depth, mask, invK, invRt):
        pass
    
    @abstractmethod
    def location_vector(self):
        """
        Get the set of 3D point locations that comprise the scene
        """
        pass

    @abstractmethod
    def create_image(self, measurements):
        """
        Yields an image with the relevant measurements filled into the correct pixels.
        """
        pass

    def implied_normal_image(self):
        location_image = self.create_image(self.location_vector()) # HxWx3
        if self.mask[-1,:].sum() + self.mask[:,-1].sum() > 0:
            error("The mask should not reach the bottom and right image edges.")
        down_vectors = normalize(location_image[:-1,:-1] - location_image[1:,:-1])
        right_vectors = normalize(location_image[:-1,:-1] - location_image[:-1,1:])
        normal_image = normalize(cross_product(down_vectors, right_vectors))
        camloc = self.invRt[:3,3:]
        # make sure the normal is the one that points towards the camera
        reorientations = inner_product(
            normal_image,
            camloc.view(1,1,3) - location_image[:-1,:-1]
        ).sign()
        normal_image = normal_image * reorientations # H-1 x W-1 x 3
        # extend the normals to the edges of the mask
        local_normal_mean = normalize(torch.nn.functional.conv2d(
            normal_image.permute(2,0,1)[:,None],
            torch.ones((1,1,5,5),device=normal_image.device),
            padding=2
        )[:,0].permute(1,2,0))

        invalid_normals = norm(normal_image) == 0
        replacement_mask = invalid_normals[:,:,0] * self.mask[:-1,:-1]
        normal_image[replacement_mask] = local_normal_mean[replacement_mask]
        # for badly connected masks, some of the pixels are currently still not filled
        # fill them just with a normal pointing roughly in the right direction
        invalid_normals = norm(normal_image) == 0
        replacement_mask = invalid_normals[:,:,0] * self.mask[:-1,:-1]
        normal_image[replacement_mask] = normalize(
            local_normal_mean.view(-1,3).sum(dim=0, keepdim=True)
        )
        normal_image = torch.cat(
            (
                normal_image,
                torch.zeros(
                    1, normal_image.shape[1], 3,
                    dtype=normal_image.dtype, device=normal_image.device
                )
            ), dim=0)
        normal_image = torch.cat(
            (
                normal_image,
                torch.zeros(
                    normal_image.shape[0], 1, 3,
                    dtype=normal_image.dtype, device=normal_image.device
                )
            ), dim=1)

        return normal_image

    def implied_normal_vector(self):
        return self.implied_normal_image()[self.mask]

    @abstractmethod
    def device(self):
        """
        Get the torch.device the parameters live on.
        """
        return self.depth.device

    @abstractmethod
    def get_point_count(self):
        """
        Get the number of 3D points in the scene
        """
        pass



class DepthMapParametrization(LocationParametrization):
    def initialize(self, depth, mask, invK, invRt):
        self.depth = torch.nn.Parameter(depth)
        self.mask = (mask.view(depth.shape[:2]) > 0).to(depth.device)
        self.invK = invK.to(depth.device)
        self.invRt = invRt.to(depth.device)
    
    def location_vector(self):
        depth_points = depth_map_to_locations(self.depth, self.invK, self.invRt)
        return depth_points[self.mask].view(-1,3)
    
    def create_image(self, measurements):
        image = torch.zeros(self.mask.shape[0], self.mask.shape[1], measurements.shape[1], device=measurements.device)
        image[self.mask] = measurements
        return image

    def get_point_count(self):
        return self.mask.sum().item()
    
    def device(self):
        return self.depth.device

    def parameters(self):
        return self.depth[self.mask], 

    def serialize(self):
        return self.depth.detach(), self.invK, self.invRt, self.mask

    def deserialize(self, *args):
        depth, invK, invRt, mask = args
        self.depth = torch.nn.Parameter(depth)
        self.invK = invK
        self.invRt = invRt
        self.mask = mask


class PlaneParametrization(LocationParametrization):
    def initialize(self, depth, mask, invK, invRt):
        self.invK = invK.to(depth.device)
        self.invRt = invRt.to(depth.device)
        self.mask = (mask.view(depth.shape[:2]) > 0).to(depth.device)

        world_points = depth_map_to_locations(depth, invK, invRt)[mask].view(-1,3)
        self.p_plane = torch.nn.Parameter(world_points.pinverse().sum(dim=1, keepdim=False))

    def camera_rays(self):
        if self._camera_rays is None:
            xs = torch.arange(0, W).float().reshape(1,W,1).to(depth.device).expand(H,W,1)
            ys = torch.arange(0, H).float().reshape(H,1,1).to(depth.device).expand(H,W,1)
            zs = torch.ones(1,1,1).to(depth.device).expand(H,W,1)
            self._camera_rays = torch.cat((xs, ys, zs), dim=2) @ self.invRt[:3,:3].T[None] @ self.invK[:3,:3].T
            self._camera_rays = self._camera_rays[self.mask].view(-1,3)
        return self.camera_rays

    def location_vector(self):
        camera_rays = self.camera_rays() # Nx3
        camera_loc = self.invRt[:3,3:].T # 1x3
        p_plane = self.p_plane.view(3,1)
        ray_lengths = (1-camera_loc @ p_plane) / (camera_rays @ p_plane) # Nx1
        locations = camera_loc + ray_lengths * camera_rays
        return locations

    def create_image(self, measurements):
        image = torch.zeros(self.mask.shape[0], self.mask.shape[1], measurements.shape[1], device=measurements.device)
        image[self.mask] = measurements
        return image

    def get_point_count(self):
        return self.mask.sum().item()
    
    def device(self):
        return self.p_plane.device

    def parameters(self):
        return self.p_plane, 

    def serialize(self):
        return self.p_plane.detach(), self.invK, self.invRt, self.mask

    def deserialize(self, *args):
        p_plane, invK, invRt, mask = args
        self.p_plane = torch.nn.Parameter(p_plane)
        self.invK = invK
        self.invRt = invRt
        self.mask = mask


def LocationParametrizationFactory(name):
    valid_dict = {
        "depth map": DepthMapParametrization,
        "plane": PlaneParametrization,
    }
    if name in valid_dict:
        return valid_dict[name]
    else:
        error("Location parametrization '%s' is not supported." % name)
