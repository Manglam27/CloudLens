import 'dart:io';
import 'package:flutter/material.dart';
import 'package:image_picker/image_picker.dart';
import 'package:amplify_flutter/amplify_flutter.dart';
import 'package:uuid/uuid.dart';
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

      // Newest first, so an image that was just saved appears at the top.
      final modified = {for (final file in imageFiles) file: file.lastModifiedSync()};
      imageFiles.sort((a, b) => modified[b]!.compareTo(modified[a]!));

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

  Future<void> _uploadImage() async {
    if (_selectedImage == null) return;

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

      final cloudImageURL = await _fetchCloudImageURL(fileName);
      if (!mounted) return;
      await _openEditor(cloudImageURL);
    } on StorageException catch (e) {
      print("Error uploading image: ${e.message}");
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('Upload failed: ${e.message}')),
        );
      }
    } catch (e) {
      print("Error uploading image: $e");
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('Upload failed: $e')),
        );
      }
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

  AlertDialog _buildLocalImageDialog(File imageFile) {
  return AlertDialog(
    content: Image.file(imageFile, fit: BoxFit.cover),
    actions: [
      Row(
        children: [
          const Spacer(),
          TextButton(
            onPressed: () {
              setState(() {
                _selectedImage = imageFile;
              });
              Navigator.pop(context);
              _uploadImage();
            },
            child: const Text('Upload and Edit'),
          ),
        ],
      ),
    ],
  );
}

  AlertDialog _buildCloudImageDialog(String imageUrl) {
    return AlertDialog(
      content: Image.network(imageUrl, fit: BoxFit.cover),
      actions: [
        Row(
          children: [
            TextButton(
              onPressed: () async => await _saveToFavorites(imageUrl),
              child: const Text('Favorite'),
            ),
            const Spacer(),
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
                              builder: (context) => _buildLocalImageDialog(selected),
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
