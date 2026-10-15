import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import AppLayout from "./components/AppLayout";
import ProtectedRoute from "./components/ProtectedRoute";
import Dashboard from "./pages/Dashboard";
import EduPreview from "./pages/EduPreview";
import Home from "./pages/Home";
import Login from "./pages/Login";
import NewProject from "./pages/NewProject";
import NewScriptProject from "./pages/NewScriptProject";
import PipelineStatus from "./pages/PipelineStatus";
import ProjectDetail from "./pages/ProjectDetail";
import ReviewDashboard from "./pages/ReviewDashboard";
import VideoEditor from "./pages/VideoEditor";
import VoiceManager from "./pages/VoiceManager";

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<Home />} />
        <Route path="/login" element={<Login />} />
        <Route
          element={
            <ProtectedRoute>
              <AppLayout />
            </ProtectedRoute>
          }
        >
          <Route path="/studio" element={<Dashboard />} />
          <Route path="/projects/new" element={<NewProject />} />
          <Route path="/projects/new-script" element={<NewScriptProject />} />
          <Route path="/projects/:projectId" element={<ProjectDetail />} />
          <Route
            path="/projects/:projectId/new-video"
            element={<NewProject />}
          />
          <Route
            path="/projects/:projectId/voices"
            element={<VoiceManager />}
          />
          <Route path="/jobs/:jobId/status" element={<PipelineStatus />} />
          <Route path="/jobs/:jobId/review" element={<ReviewDashboard />} />
          <Route path="/jobs/:jobId/editor" element={<VideoEditor />} />
          <Route path="/jobs/:jobId/educational" element={<EduPreview />} />
        </Route>
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  );
}
