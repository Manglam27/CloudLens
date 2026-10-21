import 'dart:io';
import 'package:cached_network_image/cached_network_image.dart';
import 'package:flutter/material.dart';
import 'package:image_picker/image_picker.dart';
import 'package:amplify_flutter/amplify_flutter.dart';
import 'package:uuid/uuid.dart';
import 'package:cloud_lens/Pages/check_page.dart';
import 'package:cloud_lens/Pages/editing_page.dart';
import 'package:cloud_lens/database.dart'; // For DBHelper.insertFavorite

/// A photo in the user's cloud library: its S3 path and a signed link to it.
/// Signed links change every time they are made, so the path is the cache key.
class CloudPhoto {
  final String path;
  final String url;

  const CloudPhoto(this.path, this.url);
}

class PhotosPage extends StatefulWidget {
  const PhotosPage({super.key});

  @override
  _PhotosPageState createState() => _PhotosPageState();
}

class _PhotosPageState extends State<PhotosPage> with SingleTickerProviderStateMixin {
  final _picker = ImagePicker();
  File? _selectedImage;
  List<CloudPhoto> _cloudPhotos = [];
  bool _cloudLoading = true;
  List<File> _localImages = [];

  late TabController _tabController;

  @override
  void initState() {
    super.initState();
    _tabController = TabController(length: 2, vsync: this);
    // Images can be saved to the gallery from other screens while this page
    // stays alive, so re-scan whenever the user switches to the Local tab.
    _tabController.addListener(() {
      if (!_tabController.indexIsChanging && _tabController.index == 0) {
        _loadLocalImagesFromPictures();
      }
    });
    _fetchCloudImages();
    _loadLocalImagesFromPictures();
  }

  Future<void> _loadLocalImagesFromPictures() async {
    const picturesPath = '/storage/emulated/0/Pictures';
    final picturesDir = Directory(picturesPath);

    try {
      if (!await picturesDir.exists()) return;

      final imageFiles = picturesDir.listSync().whereType<File>().where((file) {
        final ext = file.path.toLowerCase();
        return ext.endsWith('.jpg') || ext.endsWith('.jpeg') || ext.endsWith('.png');
      }).toList();

      // Oldest first, so the newest images are at the bottom of the grid.
      final modified = {for (final file in imageFiles) file: file.lastModifiedSync()};
      imageFiles.sort((a, b) => modified[a]!.compareTo(modified[b]!));

      if (!mounted) return;
      setState(() {
        _localImages = imageFiles;
      });
    } on FileSystemException catch (e) {
      print("Error reading local images: $e");
    }
  }

  /// Opens the editor, then refreshes both tabs once the user comes back,
  /// since the editor can save images to the gallery.
  Future<void> _openEditor(String imageUrl) async {
    await Navigator.push(
      context,
      MaterialPageRoute(builder: (context) => EditingImages(imageUrl: imageUrl)),
    );
    if (!mounted) return;
    await _loadLocalImagesFromPictures();
    await _fetchCloudImages();
  }

  Future<void> _pickImage() async {
    final pickedFile = await _picker.pickImage(source: ImageSource.gallery);
    if (pickedFile != null) {
      final imageFile = File(pickedFile.path);
      setState(() {
        _selectedImage = imageFile;
      });

      showDialog(
        context: context,
        builder: (context) => _buildLocalImageDialog(imageFile),
      );
    }
  }

  void _showMessage(String message) {
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(message)));
  }

  /// Uploads the selected image to S3 and returns its key, or null if it failed.
  Future<String?> _uploadSelected() async {
    if (_selectedImage == null) return null;

    try {
      final user = await Amplify.Auth.getCurrentUser();
      final userID = user.userId;
      final photoID = Uuid().v4();
      final fileName = '${userID}_$photoID.jpg';

      final bytes = await _selectedImage!.readAsBytes();

      // Upload straight to the configured S3 bucket using the signed-in
      // user's Cognito identity pool credentials.
      await Amplify.Storage.uploadData(
        data: StorageDataPayload.bytes(bytes, contentType: 'image/jpeg'),
        path: StoragePath.fromString(fileName),
      ).result;
      return fileName;
    } on StorageException catch (e) {
      print("Error uploading image: ${e.message}");
      _showMessage('Upload failed: ${e.message}');
    } catch (e) {
      print("Error uploading image: $e");
      _showMessage('Upload failed: $e');
    }
    return null;
  }

  Future<void> _uploadAndEdit() async {
    final key = await _uploadSelected();
    if (key == null || !mounted) return;
    final cloudImageURL = await _fetchCloudImageURL(key);
    if (!mounted) return;
    await _openEditor(cloudImageURL);
  }

  /// Uploads the selected image and starts a credibility check on it (FR 1.4).
  Future<void> _uploadAndCheck() async {
    final key = await _uploadSelected();
    if (key == null || !mounted) return;
    await Navigator.push(
      context,
      MaterialPageRoute(builder: (context) => CheckPage(imageKey: key)),
    );
    if (!mounted) return;
    await _fetchCloudImages();
  }

  /// Permanently deletes a photo from the phone after the user confirms.
  Future<void> _deleteLocalImage(File file) async {
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Delete photo?'),
        content: const Text('This permanently deletes the photo from your phone. It cannot be undone.'),
        actions: [
          TextButton(onPressed: () => Navigator.pop(context, false), child: const Text('Cancel')),
          TextButton(
            onPressed: () => Navigator.pop(context, true),
            child: const Text('Delete', style: TextStyle(color: Colors.red)),
          ),
        ],
      ),
    );
    if (confirmed != true || !mounted) return;
    try {
      await file.delete();
      if (!mounted) return;
      Navigator.pop(context); // close the photo dialog
      setState(() => _localImages.remove(file));
      _showMessage('Photo deleted');
    } on FileSystemException catch (e) {
      print("Error deleting local image: $e");
      // Android only lets an app delete photos it saved itself.
      _showMessage("This photo couldn't be deleted. It may belong to another app.");
    }
  }

  Future<String> _fetchCloudImageURL(String fileName) async {
    try {
      final urlOperation = await Amplify.Storage.getUrl(path: StoragePath.fromString(fileName));
      final urlResult = await urlOperation.result;
      return urlResult.url.toString();
    } catch (e) {
      print("Error fetching cloud image URL: $e");
      return "";
    }
  }

  Future<void> _fetchCloudImages() async {
    try {
      final user = await Amplify.Auth.getCurrentUser();
      final userID = user.userId;

      final operation = Amplify.Storage.list(path: StoragePath.fromString(userID));
      final result = await operation.result;

      final filteredFiles = result.items
          .where((item) => item.path.startsWith(userID))
          .toList();
      // Oldest first, so the newest uploads are at the bottom of the grid.
      final epoch = DateTime.fromMillisecondsSinceEpoch(0);
      filteredFiles.sort((a, b) => (a.lastModified ?? epoch).compareTo(b.lastModified ?? epoch));

      // Sign every link at once instead of one after another.
      final photos = await Future.wait(filteredFiles.map((file) async {
        final urlResult = await Amplify.Storage.getUrl(path: StoragePath.fromString(file.path)).result;
        return CloudPhoto(file.path, urlResult.url.toString());
      }));

      if (!mounted) return;
      setState(() => _cloudPhotos = photos);
    } catch (e) {
      print("Error fetching cloud images: $e");
    } finally {
      if (mounted) setState(() => _cloudLoading = false);
    }
  }

  Future<void> _deleteCloudImage(CloudPhoto photo) async {
    try {
      await Amplify.Storage.remove(path: StoragePath.fromString(photo.path));

      setState(() {
        _cloudPhotos.remove(photo);
      });
    } catch (e) {
      print("Error deleting image: $e");
    }
  }

  Future<void> _saveToFavorites(String url) async {
    if (url.isNotEmpty) {
      try {
        await DBHelper.insertFavorite(url);
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(content: Text("Added to Favorites")),
        );
      } catch (e) {
        print("Error saving to favorites: $e");
      }
    }
  }

  /// [canDelete] is only true for photos in the Local tab; a photo picked from
  /// the gallery is a temporary copy, so deleting it would mean nothing.
  AlertDialog _buildLocalImageDialog(File imageFile, {bool canDelete = false}) {
    void upload(Future<void> Function() then) {
      setState(() {
        _selectedImage = imageFile;
      });
      Navigator.pop(context);
      then();
    }

    // Listing the buttons directly lets the dialog wrap them on narrow screens.
    return AlertDialog(
      content: Image.file(imageFile, fit: BoxFit.cover),
      actions: [
        if (canDelete)
          TextButton(
            onPressed: () => _deleteLocalImage(imageFile),
            child: const Text('Delete', style: TextStyle(color: Colors.red)),
          ),
        TextButton(
          onPressed: () => upload(_uploadAndCheck),
          child: const Text('Upload and Check'),
        ),
        TextButton(
          onPressed: () => upload(_uploadAndEdit),
          child: const Text('Upload and Edit'),
        ),
      ],
    );
  }

  /// Opens a credibility check for a cloud photo (FR 1.4).
  Future<void> _openCheck(CloudPhoto photo) async {
    Navigator.pop(context); // close the photo dialog
    await Navigator.push(
      context,
      MaterialPageRoute(builder: (context) => CheckPage(imageKey: photo.path)),
    );
  }

  /// A cloud photo, from the phone's cache after the first download. Grid
  /// tiles use a small copy ([thumbnail]) so the grid fills in quickly.
  Widget _cloudImage(CloudPhoto photo, {bool thumbnail = false}) {
    return CachedNetworkImage(
      imageUrl: photo.url,
      cacheKey: thumbnail ? 'thumb:${photo.path}' : photo.path,
      maxWidthDiskCache: thumbnail ? 400 : null,
      memCacheWidth: thumbnail ? 300 : null,
      fit: BoxFit.cover,
      fadeInDuration: const Duration(milliseconds: 150),
      placeholder: (context, url) => Container(color: Colors.white24),
      errorWidget: (context, url, error) => const Icon(Icons.broken_image_outlined),
    );
  }

  AlertDialog _buildCloudImageDialog(CloudPhoto photo) {
    // Listing the buttons directly lets the dialog wrap them on narrow screens.
    return AlertDialog(
      content: _cloudImage(photo),
      actions: [
        TextButton(
          onPressed: () async => await _saveToFavorites(photo.url),
          child: const Text('Favorite'),
        ),
        TextButton(
          onPressed: () => _openCheck(photo),
          child: const Text('Check'),
        ),
        TextButton(
          onPressed: () => _openEditor(photo.url),
          child: const Text('Edit'),
        ),
        TextButton(
          onPressed: () async {
            await _deleteCloudImage(photo);
            if (!mounted) return;
            Navigator.pop(context);
          },
          child: const Text('Delete'),
        ),
      ],
    );
  }

  Widget _cloudTab() {
    if (_cloudLoading && _cloudPhotos.isEmpty) {
      return const Center(child: CircularProgressIndicator());
    }
    // Pull down to refresh; the empty state scrolls too so the gesture works there.
    return RefreshIndicator(
      onRefresh: _fetchCloudImages,
      child: _cloudPhotos.isEmpty
          ? ListView(children: const [
              SizedBox(height: 200),
              Center(child: Text("No cloud images available.")),
            ])
          : GridView.builder(
              padding: const EdgeInsets.all(8.0),
              gridDelegate: const SliverGridDelegateWithFixedCrossAxisCount(
                crossAxisCount: 3,
                crossAxisSpacing: 8.0,
                mainAxisSpacing: 8.0,
              ),
              itemCount: _cloudPhotos.length,
              itemBuilder: (context, index) {
                final photo = _cloudPhotos[index];
                return GestureDetector(
                  onTap: () {
                    showDialog(
                      context: context,
                      builder: (context) => _buildCloudImageDialog(photo),
                    );
                  },
                  child: _cloudImage(photo, thumbnail: true),
                );
              },
            ),
    );
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        toolbarHeight: 0,
        bottom: TabBar(
          controller: _tabController,
          tabs: const [Tab(text: "Local"), Tab(text: "Cloud")],
        ),
      ),
      body: Stack(
        children: [
          Container(
            decoration: const BoxDecoration(
              gradient: LinearGradient(
                begin: Alignment.topCenter,
                end: Alignment.bottomCenter,
                colors: [Color(0xFF8EC5FC), Color(0xFFE0C3FC)],
              ),
            ),
          ),
          TabBarView(
            controller: _tabController,
            children: [
              _localImages.isEmpty
                  ? const Center(child: Text("No local images available."))
                  : GridView.builder(
                      padding: const EdgeInsets.all(8.0),
                      gridDelegate: const SliverGridDelegateWithFixedCrossAxisCount(
                        crossAxisCount: 3,
                        crossAxisSpacing: 8.0,
                        mainAxisSpacing: 8.0,
                      ),
                      itemCount: _localImages.length,
                      itemBuilder: (context, index) {
                        return GestureDetector(
                          onTap: () {
                            final selected = _localImages[index];
                            showDialog(
                              context: context,
                              builder: (context) => _buildLocalImageDialog(selected, canDelete: true),
                            );
                          },
                          child: Image.file(_localImages[index], fit: BoxFit.cover),
                        );
                      },
                    ),
              _cloudTab(),
            ],
          ),
        ],
      ),
      floatingActionButton: FloatingActionButton(
        onPressed: _pickImage,
        tooltip: 'Select Image',
        child: const Icon(Icons.add_a_photo),
      ),
    );
  }

  @override
  void dispose() {
    _tabController.dispose();
    super.dispose();
  }
}
