import hashlib

class ImageComparer():
    def compare(self, image1, image2):
        if image1.startswith("data:image/png;base64,"):
            image1 = image1.split(",", 1)[1]
        image1 = hashlib.sha1(image1).hexdigest()

        if image2.startswith("data:image/png;base64,"):
            image2 = image2.split(",", 1)[1]
        image2 = hashlib.sha1(image2).hexdigest()

        return image1 == image2;
