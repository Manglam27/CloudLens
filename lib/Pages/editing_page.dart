import 'dart:convert';
import 'dart:typed_data';
import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;
import 'package:image_gallery_saver_plus/image_gallery_saver_plus.dart';
import 'package:flutter_image_compress/flutter_image_compress.dart';
import 'package:amplify_flutter/amplify_flutter.dart';
import 'package:uuid/uuid.dart';
import 'package:cloud_lens/database.dart'; // only if you use _saveToFavorites

class EditingImages extends StatefulWidget {
  final String imageUrl;

  const EditingImages({Key? key, required this.imageUrl}) : super(key: key);

  @override
  State<EditingImages> createState() => _EditingImagesState();
}

class _EditingImagesState extends State<EditingImages> {
  Uint8List? editedImageBytes;
  String? editedImageUrl;
  bool isLoading = false;

  /// S3 key of the edit on screen while it is still an unsaved preview in the
  /// temporary edited/ folder. It is deleted unless the user saves it.
  String? _previewKey;

  /// The in-progress or finished move of the current edit into the library.
  Future<void>? _keepFuture;

  final String lambdaEndpoint = 'https://ieip1diyzc.execute-api.us-east-1.amazonaws.com/photoEdits';

  final List<String> editOptions = [
    'invert',
    'grayscale',
    'blur',
    'edge',
    'flip',
    'brightness',
    'contrast',
    'sharpen',
    'sepia',
    'pencil',
    'threshold',
    'emboss',
  ];

  Future<Uint8List> _compressImage(Uint8List originalBytes) async {
    return await FlutterImageCompress.compressWithList(
      originalBytes,
      quality: 75,
      minWidth: 1024,
      minHeight: 1024,
    );
  }

  Future<void> _applyEdit(String operation) async {
    setState(() => isLoading = true);

    try {
      final originalResponse = await http.get(Uri.parse(widget.imageUrl));
      if (originalResponse.statusCode != 200) {
        print('Failed to load original image');
        setState(() => isLoading = false);
        return;
      }

      final compressedBytes = await _compressImage(originalResponse.bodyBytes);
      final base64Body = base64Encode(compressedBytes);

      final fileName = '${operation}_${DateTime.now().millisecondsSinceEpoch}.jpg';

      final payload = {
        'file_name': fileName,
        'body': base64Body,
        'operation': operation,
      };

      final response = await http.post(
        Uri.parse(lambdaEndpoint),
        headers: {'Content-Type': 'application/json'},
        body: jsonEncode(payload),
      );

      if (response.statusCode == 200) {
        final body = jsonDecode(response.body);
        final newPreviewKey = body['key'] as String?;
        if (!mounted) {
          // The user left while the edit was being made, so nobody wants it.
          if (newPreviewKey != null) _discardPreview(newPreviewKey);
          return;
        }
        final previousPreviewKey = _previewKey;
        setState(() {
          editedImageUrl = body['url'];
          editedImageBytes = null; // Clear memory version when switching to network
          _previewKey = newPreviewKey;
          _keepFuture = null;
        });
        // The user moved on from the previous filter without saving it.
        if (previousPreviewKey != null) _discardPreview(previousPreviewKey);
      } else {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text("❌ Failed: ${response.body}")),
        );
      }
    } catch (e) {
      print('Error calling Lambda: $e');
    }

    if (mounted) setState(() => isLoading = false);
  }

  @override
  void dispose() {
    // Leaving without saving: the preview on screen is not wanted any more.
    final previewKey = _previewKey;
    if (previewKey != null) _discardPreview(previewKey);
    super.dispose();
  }

  /// Deletes an unsaved edit preview from S3. Runs in the background; if it
  /// fails, the bucket's lifecycle rule removes the preview within a day.
  void _discardPreview(String key) {
    Amplify.Storage.remove(path: StoragePath.fromString(key)).result.then(
      (_) {},
      onError: (Object e) => print('Could not discard edit preview $key: $e'),
    );
  }

  /// Keeps the edit on screen: moves it out of the temporary edited/ folder
  /// into the user's library, where it shows up in the Cloud tab. Safe to call
  /// more than once; the same edit is only moved once.
  Future<void> _keepCurrentEdit() => _keepFuture ??= _moveEditToLibrary();

  Future<void> _moveEditToLibrary() async {
    final previewKey = _previewKey;
    if (previewKey == null) return; // Original photo, or already kept.

    // Claim it first, so closing the page mid-move does not delete it.
    _previewKey = null;
    setState(() => isLoading = true);
    try {
      final user = await Amplify.Auth.getCurrentUser();
      final libraryKey = '${user.userId}_${const Uuid().v4()}.jpg';
      await Amplify.Storage.copy(
        source: StoragePath.fromString(previewKey),
        destination: StoragePath.fromString(libraryKey),
      ).result;
      final urlResult =
          await Amplify.Storage.getUrl(path: StoragePath.fromString(libraryKey)).result;
      _discardPreview(previewKey);
      if (mounted) setState(() => editedImageUrl = urlResult.url.toString());
    } catch (e) {
      // Put it back so it is still cleaned up later and a retry can keep it.
      _previewKey ??= previewKey;
      _keepFuture = null;
      rethrow;
    } finally {
      if (mounted) setState(() => isLoading = false);
    }
  }

  void _showMessage(String message) {
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(message)));
  }

  Future<void> _saveImage() async {
    if (editedImageBytes == null && editedImageUrl == null) return;
    try {
      await _keepCurrentEdit();
    } catch (e) {
      print('Could not keep edit: $e');
      _showMessage("❌ Could not save the edit. Please try again.");
      return;
    }
    try {
      Uint8List bytes;
      if (editedImageBytes != null) {
        bytes = editedImageBytes!;
      } else {
        final response = await http.get(Uri.parse(editedImageUrl!));
        bytes = response.bodyBytes;
      }
      await ImageGallerySaverPlus.saveImage(
        bytes,
        name: 'cloudlens_${DateTime.now().millisecondsSinceEpoch}',
        quality: 100,
      );
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text("Successfully saved to gallery")),
      );
    } catch (e) {
      print('Save error: $e');
    }
  }

  Future<void> _saveToFavorites() async {
    try {
      // A favorite stores the image's cloud link, so the edit must be kept.
      await _keepCurrentEdit();
    } catch (e) {
      print('Could not keep edit: $e');
      _showMessage("❌ Could not save the edit. Please try again.");
      return;
    }
    String urlToSave = editedImageUrl ?? widget.imageUrl;
    print('Attempting to save URL: $urlToSave'); // Debugging line
    if (urlToSave.isNotEmpty) {
      try {
        await DBHelper.insertFavorite(urlToSave);
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(content: Text("Added to Favorites")),
        );
      } catch (e) {
        print("Error saving to favorites: $e");
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(content: Text("❌ Failed to add to favorites")),
        );
      }
    } else {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text("❌ No image to save to favorites")),
      );
    }
  }

  @override
  Widget build(BuildContext context) {
    final displayImage = editedImageBytes != null
        ? Image.memory(editedImageBytes!)
        : editedImageUrl != null
            ? Image.network(editedImageUrl!)
            : Image.network(widget.imageUrl);

    return Scaffold(
      appBar: AppBar(
        title: const Text('Edit Image'),
        backgroundColor: const Color.fromARGB(255, 84, 152, 247),
        foregroundColor: Colors.white,
        centerTitle: true,
      ),
      body: Padding(
        padding: const EdgeInsets.all(16.0),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Expanded(child: displayImage),
            const SizedBox(height: 16),
            ElevatedButton.icon(
              onPressed: _saveImage,
              icon: const Icon(Icons.download),
              label: const Text("Save to Gallery"),
              style: _buttonStyle(),
            ),
            const SizedBox(height: 10),
            ElevatedButton.icon(
              onPressed: _saveToFavorites,
              icon: const Icon(Icons.favorite),
              label: const Text("Add to Favorites"),
              style: _buttonStyle(color: Colors.pinkAccent),
            ),
            const SizedBox(height: 20),
            const Text("Choose a Filter:", style: TextStyle(fontSize: 18, fontWeight: FontWeight.bold)),
            const SizedBox(height: 12),
            Expanded(
              child: SingleChildScrollView(
                child: Column(
                  children: editOptions.map((method) {
                    return Padding(
                      padding: const EdgeInsets.symmetric(vertical: 4.0),
                      child: ElevatedButton(
                        onPressed: isLoading ? null : () => _applyEdit(method),
                        child: Text(
                          method.toUpperCase(),
                          style: const TextStyle(fontWeight: FontWeight.bold),
                        ),
                        style: _buttonStyle(),
                      ),
                    );
                  }).toList(),
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }

  ButtonStyle _buttonStyle({Color color = const Color.fromARGB(255, 84, 152, 247)}) {
    return ElevatedButton.styleFrom(
      minimumSize: const Size(double.infinity, 50),
      backgroundColor: color,
      foregroundColor: Colors.white,
      elevation: 5,
      textStyle: const TextStyle(
        fontSize: 16,
        fontWeight: FontWeight.bold,
      ),
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(12),
      ),
    );
  }
}
