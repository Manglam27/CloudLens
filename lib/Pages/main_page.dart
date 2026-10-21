import 'package:camera/camera.dart';
import 'package:cloud_lens/Pages/camera_page.dart';
import 'package:cloud_lens/Pages/favorites_page.dart';
import 'package:cloud_lens/Pages/history_page.dart';
import 'package:cloud_lens/Pages/photos_page.dart';
import 'package:flutter/material.dart';
import 'package:google_fonts/google_fonts.dart';
import 'package:amplify_flutter/amplify_flutter.dart';

class MainPage extends StatefulWidget {
  final Future<void> Function(BuildContext) signOutCallback;

  const MainPage({super.key, required this.signOutCallback});
  
  @override
  State<MainPage> createState() => _MainPageState();
}

class _MainPageState extends State<MainPage> {
  List<CameraDescription> cameras = [];
  // Only the Camera tab waits for this. Blocking the whole page on it left the
  // app spinning forever on iPhones (and simulators) where no camera is found.
  bool _camerasLoaded = false;
  int _pageIndex = 0;
  String _userEmail = '';

  Future<void> initializeCameras() async {
    try {
      cameras = await availableCameras();
    } catch (e) {
      print("Error initializing cameras: $e");
    }
    if (!mounted) return;
    setState(() => _camerasLoaded = true);
  }

  Widget _cameraTab() {
    if (!_camerasLoaded) return const Center(child: CircularProgressIndicator());
    if (cameras.isEmpty) {
      return const Center(
        child: Padding(
          padding: EdgeInsets.all(24),
          child: Text('No camera is available on this device. You can still add photos from the gallery.',
              textAlign: TextAlign.center),
        ),
      );
    }
    return CameraPage(cameras: cameras);
  }

  // Get current user's email
  Future<void> _getCurrentUser() async {
    try {
      final userAttributes = await Amplify.Auth.fetchUserAttributes();
      final emailAttribute = userAttributes.firstWhere(
        (attribute) => attribute.userAttributeKey == CognitoUserAttributeKey.email);
      if (!mounted) return;
      setState(() {
        _userEmail = emailAttribute.value; // Set the user's email
      });
    } catch (e) {
      print("Error getting current user: $e");
    }
  }

  @override
  void initState() {
    super.initState();
    initializeCameras();
    _getCurrentUser();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: Container(
        decoration: const BoxDecoration(
          gradient: LinearGradient(
            colors: [Color(0xFFE0F7FA), Color(0xFFFFFFFF)],
            begin: Alignment.topCenter,
            end: Alignment.bottomCenter,
          ),
        ),
        child: [
          const PhotosPage(),
          const HistoryPage(),
          const FavoritesPage(),
          _cameraTab(),
        ][_pageIndex], // Display the selected page based on _pageIndex
      ),
      appBar: AppBar(
        title: Text(_userEmail.isNotEmpty ? _userEmail : "Loading..."),
        actions: [
          IconButton(
            icon: Icon(Icons.logout),
            onPressed: () async {
              await widget.signOutCallback(context); // Sign out logic
            },
          ),
        ],
      ),
      bottomNavigationBar: Container(
        decoration: const BoxDecoration(
          color: Colors.white,
          boxShadow: [
            BoxShadow(color: Colors.black12, blurRadius: 8),
          ],
        ),
        child: BottomNavigationBar(
          backgroundColor: Colors.white,
          selectedItemColor: Colors.deepPurple,
          unselectedItemColor: Colors.deepPurple.shade200,
          selectedLabelStyle: GoogleFonts.poppins(fontWeight: FontWeight.w600),
          unselectedLabelStyle: GoogleFonts.poppins(fontWeight: FontWeight.w400),
          type: BottomNavigationBarType.fixed,
          items: const [
            BottomNavigationBarItem(icon: Icon(Icons.image), label: "Photos"),
            BottomNavigationBarItem(icon: Icon(Icons.fact_check), label: "History"),
            BottomNavigationBarItem(icon: Icon(Icons.star), label: "Favorites"),
            BottomNavigationBarItem(icon: Icon(Icons.camera_alt), label: "Camera"),
          ],
          onTap: (index) {
            setState(() {
              _pageIndex = index; // Switch pages based on the tab clicked
            });
          },
          currentIndex: _pageIndex, // Ensure the correct tab is highlighted
        ),
      ),
    );
  }
}
