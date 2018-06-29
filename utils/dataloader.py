import glob
import os
from PIL import Image
from tensorflow.python.keras.utils import Sequence


class COCOSequence(Sequence):

  def __init__(self, img_directory, captions_file, batch_size):
    self.images = glob.glob(os.path.join(img_directory, '*.jpg'))

    self.x, self.y = x_set, y_set
    self.batch_size = batch_size

  def __len__(self):
    return math.ceil(len(self.x) / self.batch_size)

  def __getitem__(self, idx):
    batch_x = self.x[idx * self.batch_size:(idx + 1) *
                     self.batch_size]
    batch_y = self.y[idx * self.batch_size:(idx + 1) *
                     self.batch_size]

    return np.array([
        resize(imread(file_name), (200, 200))
        for file_name in batch_x]), np.array(batch_y)
