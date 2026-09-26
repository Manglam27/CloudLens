import 'dart:io';
import 'package:flutter/material.dart';
import 'package:image_picker/image_picker.dart';
import 'package:amplify_flutter/amplify_flutter.dart';
import 'package:uuid/uuid.dart';
import 'package:cloud_lens/Pages/check_page.dart';
import 'package:cloud_lens/Pages/editing_page.dart';
import 'package:cloud_lens/database.dart'; // For DBHelper.insertFavorite

class PhotosPage extends StatefulWidget {
  const PhotosPage({super.key});

  @override
  _PhotosPageState createState() => _PhotosPageState();
}

class _PhotosPageState extends State<PhotosPage> with SingleTickerProviderStateMixin {
  final _picker = ImagePicker();
  File? _selectedImage;
  List<String> _cloudImageURLs = [];
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

      final List<String> imageUrls = [];
      for (var file in filteredFiles) {
        final urlOperation = await Amplify.Storage.getUrl(path: StoragePath.fromString(file.path));
        final urlResult = await urlOperation.result;
        imageUrls.add(urlResult.url.toString());
      }

      setState(() {
        _cloudImageURLs = imageUrls;
      });
    } catch (e) {
      print("Error fetching cloud images: $e");
    }
  }

  Future<void> _deleteCloudImage(String fileUrl) async {
    try {
      Uri uri = Uri.parse(fileUrl);
      String path = uri.path;
      String fileName = path.split('/').last;

      await Amplify.Storage.remove(path: StoragePath.fromString(fileName));

      setState(() {
        _cloudImageURLs.remove(fileUrl);
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

  /// Opens a credibility check for a cloud photo (FR 1.4). The S3 key is the
  /// last path segment of the photo's signed URL.
  Future<void> _openCheck(String imageUrl) async {
    final imageKey = Uri.parse(imageUrl).pathSegments.last;
    Navigator.pop(context); // close the photo dialog
    await Navigator.push(
      context,
      MaterialPageRoute(builder: (context) => CheckPage(imageKey: imageKey)),
    );
  }

  AlertDialog _buildCloudImageDialog(String imageUrl) {
    // Listing the buttons directly lets the dialog wrap them on narrow screens.
    return AlertDialog(
      content: Image.network(imageUrl, fit: BoxFit.cover),
      actions: [
        TextButton(
          onPressed: () async => await _saveToFavorites(imageUrl),
          child: const Text('Favorite'),
        ),
        TextButton(
          onPressed: () => _openCheck(imageUrl),
          child: const Text('Check'),
        ),
        TextButton(
          onPressed: () => _openEditor(imageUrl),
          child: const Text('Edit'),
        ),
        TextButton(
          onPressed: () async {
            await _deleteCloudImage(imageUrl);
            Navigator.pop(context);
          },
          child: const Text('Delete'),
        ),
      ],
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
              _cloudImageURLs.isEmpty
                  ? const Center(child: Text("No cloud images available."))
                  : GridView.builder(
                      padding: const EdgeInsets.all(8.0),
                      gridDelegate: const SliverGridDelegateWithFixedCrossAxisCount(
                        crossAxisCount: 3,
                        crossAxisSpacing: 8.0,
                        mainAxisSpacing: 8.0,
                      ),
                      itemCount: _cloudImageURLs.length,
                      itemBuilder: (context, index) {
                        return GestureDetector(
                          onTap: () {
                            showDialog(
                              context: context,
                              builder: (context) => _buildCloudImageDialog(_cloudImageURLs[index]),
                            );
                          },
                          child: Image.network(_cloudImageURLs[index], fit: BoxFit.cover),
                        );
                      },
                    ),
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
